from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from productflow_backend.config import get_runtime_settings
from productflow_backend.domain.durable_generation_tasks import (
    IMAGE_SESSION_GENERATION_TASK_CONTRACT,
    WORKFLOW_RUN_GENERATION_TASK_CONTRACT,
)
from productflow_backend.domain.enums import WorkflowNodeStatus
from productflow_backend.infrastructure.db.models import ImageSessionGenerationTask, WorkflowNodeRun, WorkflowRun

GENERATION_CAPACITY_LOCK_KEY = 42630001


@dataclass(frozen=True, slots=True)
class GenerationQueueOverview:
    active_count: int
    running_count: int
    queued_count: int
    max_concurrent_tasks: int


@dataclass(frozen=True, slots=True)
class GenerationTaskQueueMetadata:
    overview: GenerationQueueOverview
    queued_ahead_count: int | None
    queue_position: int | None


def _active_async_task_count(session: Session) -> int:
    active_workflow_runs = session.scalar(
        select(func.count())
        .select_from(WorkflowRun)
        .where(WorkflowRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.active_statuses))
    )
    active_image_session_tasks = session.scalar(
        select(func.count())
        .select_from(ImageSessionGenerationTask)
        .where(ImageSessionGenerationTask.status.in_(IMAGE_SESSION_GENERATION_TASK_CONTRACT.active_statuses))
    )
    return int(active_workflow_runs or 0) + int(active_image_session_tasks or 0)


def _running_async_task_count(session: Session) -> int:
    running_workflow_node_runs = session.scalar(
        select(func.count())
        .select_from(WorkflowNodeRun)
        .join(WorkflowRun, WorkflowRun.id == WorkflowNodeRun.workflow_run_id)
        .where(
            WorkflowRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.active_statuses),
            WorkflowNodeRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.execution_running_statuses),
        )
    )
    running_image_session_tasks = session.scalar(
        select(func.count())
        .select_from(ImageSessionGenerationTask)
        .where(ImageSessionGenerationTask.status.in_(IMAGE_SESSION_GENERATION_TASK_CONTRACT.running_statuses))
    )
    return int(running_workflow_node_runs or 0) + int(running_image_session_tasks or 0)


def _status_count(session: Session, model: type, statuses: tuple[StrEnum, ...]) -> int:
    count = session.scalar(select(func.count()).select_from(model).where(model.status.in_(statuses)))
    return int(count or 0)


def _workflow_run_queue_status_counts(session: Session) -> tuple[int, int]:
    runs = session.scalars(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.node_runs))
        .where(WorkflowRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.active_statuses))
    ).all()
    running_count = 0
    queued_count = 0
    for run in runs:
        has_running_node_run = any(
            WORKFLOW_RUN_GENERATION_TASK_CONTRACT.execution_is_running(node_run.status) for node_run in run.node_runs
        )
        if has_running_node_run:
            running_count += 1
            continue
        has_queued_node_run = any(
            WORKFLOW_RUN_GENERATION_TASK_CONTRACT.execution_is_queued(node_run.status) for node_run in run.node_runs
        )
        if has_queued_node_run:
            queued_count += 1
            continue
        if run.node_runs and all(node_run.status == WorkflowNodeStatus.SUCCEEDED for node_run in run.node_runs):
            queued_count += 1
    return running_count, queued_count


def get_generation_queue_overview(session: Session) -> GenerationQueueOverview:
    """Return the global durable generation queue snapshot."""

    workflow_running_count, workflow_queued_count = _workflow_run_queue_status_counts(session)
    running_count = workflow_running_count + _status_count(
        session,
        ImageSessionGenerationTask,
        IMAGE_SESSION_GENERATION_TASK_CONTRACT.running_statuses,
    )
    queued_count = workflow_queued_count + _status_count(
        session,
        ImageSessionGenerationTask,
        IMAGE_SESSION_GENERATION_TASK_CONTRACT.queued_statuses,
    )
    return GenerationQueueOverview(
        active_count=running_count + queued_count,
        running_count=running_count,
        queued_count=queued_count,
        max_concurrent_tasks=get_runtime_settings().generation_max_concurrent_tasks,
    )


def get_queued_generation_positions(session: Session) -> dict[str, int]:
    queued_items: list[tuple[datetime, str, str]] = []
    workflow_run_queued_at: dict[str, datetime] = {}
    for run_id, started_at in session.execute(
        select(WorkflowRun.id, WorkflowNodeRun.started_at)
        .join(WorkflowNodeRun, WorkflowNodeRun.workflow_run_id == WorkflowRun.id)
        .where(
            WorkflowRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.active_statuses),
            WorkflowNodeRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.execution_queued_statuses),
        )
    ).all():
        current = workflow_run_queued_at.get(run_id)
        if current is None or started_at < current:
            workflow_run_queued_at[run_id] = started_at
    for run in session.scalars(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.node_runs))
        .where(WorkflowRun.status.in_(WORKFLOW_RUN_GENERATION_TASK_CONTRACT.active_statuses))
    ):
        if run.id in workflow_run_queued_at:
            continue
        if run.node_runs and all(node_run.status == WorkflowNodeStatus.SUCCEEDED for node_run in run.node_runs):
            workflow_run_queued_at[run.id] = run.started_at
    queued_items.extend(
        (started_at, "product_workflow", run_id) for run_id, started_at in workflow_run_queued_at.items()
    )
    queued_items.extend(
        (task.created_at, "image_session", task.id)
        for task in session.scalars(
            select(ImageSessionGenerationTask).where(
                ImageSessionGenerationTask.status.in_(IMAGE_SESSION_GENERATION_TASK_CONTRACT.queued_statuses)
            )
        ).all()
    )
    queued_items.sort(key=lambda item: (item[0], item[1], item[2]))
    return {item_id: index + 1 for index, (_created_at, _kind, item_id) in enumerate(queued_items)}


def get_workflow_run_queue_metadata(
    session: Session,
    run: WorkflowRun,
    *,
    overview: GenerationQueueOverview | None = None,
    queued_positions: dict[str, int] | None = None,
) -> GenerationTaskQueueMetadata:
    overview = overview or get_generation_queue_overview(session)
    queued_ahead_count: int | None = None
    queue_position: int | None = None
    has_queued_work = any(
        WORKFLOW_RUN_GENERATION_TASK_CONTRACT.execution_is_queued(node_run.status) for node_run in run.node_runs
    ) or (bool(run.node_runs) and all(node_run.status == WorkflowNodeStatus.SUCCEEDED for node_run in run.node_runs))
    if WORKFLOW_RUN_GENERATION_TASK_CONTRACT.is_active(run.status) and has_queued_work:
        positions = queued_positions or get_queued_generation_positions(session)
        queue_position = positions.get(run.id)
        if queue_position is not None:
            queued_ahead_count = max(0, queue_position - 1)
    return GenerationTaskQueueMetadata(
        overview=overview,
        queued_ahead_count=queued_ahead_count,
        queue_position=queue_position,
    )


def get_generation_task_queue_metadata(
    session: Session,
    task: ImageSessionGenerationTask,
    *,
    overview: GenerationQueueOverview | None = None,
    queued_positions: dict[str, int] | None = None,
) -> GenerationTaskQueueMetadata:
    overview = overview or get_generation_queue_overview(session)
    queued_ahead_count: int | None = None
    queue_position: int | None = None
    if IMAGE_SESSION_GENERATION_TASK_CONTRACT.is_queued(task.status):
        positions = queued_positions or get_queued_generation_positions(session)
        queue_position = positions.get(task.id)
        if queue_position is not None:
            queued_ahead_count = max(0, queue_position - 1)
    return GenerationTaskQueueMetadata(
        overview=overview,
        queued_ahead_count=queued_ahead_count,
        queue_position=queue_position,
    )


def active_generation_task_count(session: Session) -> int:
    """Return globally active provider/worker work from durable DB rows."""

    return _active_async_task_count(session)


def _lock_generation_capacity(session: Session) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(GENERATION_CAPACITY_LOCK_KEY)))


def generation_running_capacity_available(session: Session) -> bool:
    """Serialize worker claim checks and return whether one more task may enter provider execution."""

    _lock_generation_capacity(session)
    limit = get_runtime_settings().generation_max_concurrent_tasks
    return _running_async_task_count(session) < limit


def ensure_generation_capacity(session: Session) -> None:
    """Lock capacity bookkeeping without rejecting durable queued submissions.

    `generation_max_concurrent_tasks` limits worker entry into provider/running execution, not durable task admission.
    Keeping this entrypoint as a no-op compatibility shim avoids callers turning queue depth into a submit-time 429.
    """

    _lock_generation_capacity(session)
