"""P3 覆盖率补测：queue.py 投递函数与恢复异常路径。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from productflow_backend.domain.durable_generation_tasks import (
    IMAGE_SESSION_GENERATION_TASK_CONTRACT,
)
from productflow_backend.infrastructure import queue as queue_module
from productflow_backend.infrastructure.db.models import ImageSessionGenerationTask


class RecordingActor:
    def __init__(self, name: str, sink: list, fail: bool = False) -> None:
        self._name = name
        self._sink = sink
        self._fail = fail

    def send(self, *args):
        if self._fail:
            raise RuntimeError("broker down")
        self._sink.append((self._name, args))
        return "message"

    def send_with_options(self, args=None, delay=None, **kwargs):
        if self._fail:
            raise RuntimeError("broker down")
        self._sink.append((self._name, args, delay))
        return "message"


@pytest.fixture()
def delivery(monkeypatch):
    """记录所有 actor 投递；get_broker 替换为哑对象（不触 Redis）。"""
    sink: list = []
    monkeypatch.setattr(queue_module, "get_broker", lambda: object())
    actors = {
        "run_product_workflow_run": RecordingActor("run_product_workflow_run", sink),
        "run_product_workflow_node_run": RecordingActor("run_product_workflow_node_run", sink),
        "run_image_session_generation_task": RecordingActor("run_image_session_generation_task", sink),
    }
    for name, actor in actors.items():
        monkeypatch.setattr(f"productflow_backend.workers.{name}", actor)
    return sink


def test_enqueue_functions_dispatch_to_actors(configured_env, delivery, monkeypatch) -> None:
    monkeypatch.setattr(
        queue_module,
        "get_image_session_stale_running_after",
        lambda: timedelta(minutes=30),
    )

    queue_module.enqueue_workflow_run("run-1")
    queue_module.enqueue_workflow_run_later("run-2", delay_ms=500)
    queue_module.enqueue_workflow_node_run("node-1")
    queue_module.enqueue_workflow_node_run_later("node-2", delay_ms=500)
    queue_module.enqueue_image_session_generation_task("task-1")
    queue_module.enqueue_image_session_generation_task_later("task-2", delay_ms=500)

    names = [item[0] for item in delivery]
    assert names == [
        "run_product_workflow_run",
        "run_product_workflow_run",
        "run_product_workflow_node_run",
        "run_product_workflow_node_run",
        "run_image_session_generation_task",
        "run_image_session_generation_task",
    ]
    # 带 delay 的投递第三个元素是延迟毫秒
    delayed = [item for item in delivery if len(item) == 3]
    assert {item[2] for item in delayed} == {500}


def test_recovery_enqueues_are_counted(configured_env, delivery, db_session) -> None:
    from productflow_backend.infrastructure.db.models import ImageSession

    image_session = ImageSession(title="恢复统计")
    db_session.add(image_session)
    db_session.flush()
    task = ImageSessionGenerationTask(
        session_id=image_session.id,
        status=IMAGE_SESSION_GENERATION_TASK_CONTRACT.queued_statuses[0],
        prompt="测试",
        size="1024x1024",
        generation_count=1,
        is_retryable=True,
    )
    db_session.add(task)
    db_session.commit()

    summary = queue_module.recover_unfinished_image_session_generation_tasks(
        stale_running_after=timedelta(minutes=30)
    )
    assert summary.queued_tasks == 1
    assert summary.enqueued_tasks == 1
    assert len(delivery) == 1


def test_recovery_enqueue_failure_is_swallowed_per_task(
    configured_env, db_session, monkeypatch
) -> None:
    """入队失败不抛异常、只记日志并计入 enqueued=0。"""
    from productflow_backend.infrastructure.db.models import ImageSession

    sink: list = []
    failing = RecordingActor("run_image_session_generation_task", sink, fail=True)
    monkeypatch.setattr("productflow_backend.workers.run_image_session_generation_task", failing)

    image_session = ImageSession(title="入队失败")
    db_session.add(image_session)
    db_session.flush()
    task = ImageSessionGenerationTask(
        session_id=image_session.id,
        status=IMAGE_SESSION_GENERATION_TASK_CONTRACT.queued_statuses[0],
        prompt="测试",
        size="1024x1024",
        generation_count=1,
        is_retryable=True,
    )
    db_session.add(task)
    db_session.commit()

    summary = queue_module.recover_unfinished_image_session_generation_tasks(
        stale_running_after=timedelta(minutes=30)
    )
    assert summary.queued_tasks == 1
    assert summary.enqueued_tasks == 0


def test_recovery_swallows_database_errors(configured_env, monkeypatch) -> None:
    """数据库异常时返回空摘要并回滚（启动不因恢复失败而崩溃）。"""

    class BrokenSession:
        def scalars(self, *args, **kwargs):
            raise RuntimeError("db gone")

        def rollback(self):
            pass

        def close(self):
            pass

    class BrokenFactory:
        def __call__(self):
            return BrokenSession()

    monkeypatch.setattr(queue_module, "get_session_factory", BrokenFactory)
    empty = queue_module.ImageSessionGenerationTaskRecoverySummary()
    assert queue_module.recover_unfinished_image_session_generation_tasks() == empty
    assert queue_module.recover_unfinished_workflow_runs() == queue_module.WorkflowRunRecoverySummary()


def test_as_aware_utc_handles_naive_datetimes() -> None:
    naive = datetime(2026, 9, 6, 12, 0, 0)
    aware = queue_module._as_aware_utc(naive)
    assert aware.tzinfo is UTC
    assert queue_module._as_aware_utc(aware) is aware
