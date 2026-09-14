from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import dramatiq

from productflow_backend.application.image_sessions import execute_image_session_generation_task
from productflow_backend.application.product_workflows import (
    execute_product_workflow_node_run,
    execute_product_workflow_run,
)
from productflow_backend.config import get_runtime_settings
from productflow_backend.domain.durable_generation_tasks import (
    IMAGE_SESSION_GENERATION_TASK_CONTRACT,
    WORKFLOW_RUN_GENERATION_TASK_CONTRACT,
    assert_actor_uses_durable_generation_contract,
)
from productflow_backend.infrastructure.logging import (
    cleanup_old_logs,
    configure_logging,
    reset_image_session_generation_task_id,
    reset_workflow_node_run_id,
    reset_workflow_run_id,
    set_image_session_generation_task_id,
    set_workflow_node_run_id,
    set_workflow_run_id,
)
from productflow_backend.infrastructure.queue import (
    get_broker,
    recover_unfinished_image_session_generation_tasks,
    recover_unfinished_workflow_runs,
)
from productflow_backend.infrastructure.storage_cleanup import run_cleanup

configure_logging()
get_broker()

logger = logging.getLogger(__name__)

# 队列周期对账间隔（秒）：运行中 Redis 丢消息/滞留时由 DB（authoritative state）兜底补发
DEFAULT_RECONCILE_INTERVAL_SECONDS = 1800

# 存储生命周期清理间隔（秒）：只清无 DB 引用的孤儿文件与过期导出，默认每天一次
DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS = 86400
DEFAULT_STORAGE_CLEANUP_INITIAL_DELAY_SECONDS = 600


def get_image_session_worker_failsafe_time_limit_ms() -> int:
    return int(get_runtime_settings().image_session_worker_failsafe_time_limit_minutes) * 60 * 1000


def get_product_workflow_worker_failsafe_time_limit_ms() -> int:
    return get_image_session_worker_failsafe_time_limit_ms()


IMAGE_SESSION_WORKER_FAILSAFE_TIME_LIMIT_MS = get_image_session_worker_failsafe_time_limit_ms()
PRODUCT_WORKFLOW_WORKER_FAILSAFE_TIME_LIMIT_MS = get_product_workflow_worker_failsafe_time_limit_ms()


@dramatiq.actor(max_retries=0, time_limit=PRODUCT_WORKFLOW_WORKER_FAILSAFE_TIME_LIMIT_MS)
def run_product_workflow_run(workflow_run_id: str) -> None:
    """商品工作流 scheduler：发现 ready 节点并派发独立节点任务。"""
    token = set_workflow_run_id(workflow_run_id)
    try:
        execute_product_workflow_run(workflow_run_id)
    finally:
        reset_workflow_run_id(token)


@dramatiq.actor(max_retries=0, time_limit=PRODUCT_WORKFLOW_WORKER_FAILSAFE_TIME_LIMIT_MS)
def run_product_workflow_node_run(workflow_node_run_id: str) -> None:
    """商品工作流节点 worker：执行单个 WorkflowNodeRun，完成后唤醒 scheduler。"""
    token = set_workflow_node_run_id(workflow_node_run_id)
    try:
        execute_product_workflow_node_run(workflow_node_run_id)
    finally:
        reset_workflow_node_run_id(token)


@dramatiq.actor(max_retries=0, time_limit=IMAGE_SESSION_WORKER_FAILSAFE_TIME_LIMIT_MS)
def run_image_session_generation_task(task_id: str) -> None:
    """连续生图 worker：执行失败落库为通用安全错误。"""
    token = set_image_session_generation_task_id(task_id)
    try:
        execute_image_session_generation_task(task_id)
    finally:
        reset_image_session_generation_task_id(token)


assert_actor_uses_durable_generation_contract(WORKFLOW_RUN_GENERATION_TASK_CONTRACT, run_product_workflow_run)
assert_actor_uses_durable_generation_contract(
    IMAGE_SESSION_GENERATION_TASK_CONTRACT,
    run_image_session_generation_task,
)


def _running_under_dramatiq_cli() -> bool:
    return any(Path(arg).name == "dramatiq" for arg in sys.argv)


def get_reconcile_interval_seconds() -> int:
    """队列周期对账间隔（秒）：读环境变量 RECONCILE_INTERVAL_SECONDS，默认 1800，非法值回退默认。"""
    raw = os.getenv("RECONCILE_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    try:
        interval = int(raw)
    except ValueError:
        logger.warning("RECONCILE_INTERVAL_SECONDS 不是合法整数，使用默认值: %s", raw)
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    return interval if interval > 0 else DEFAULT_RECONCILE_INTERVAL_SECONDS


def _reconcile_queue_once() -> None:
    """对账一次：把运行中滞留/丢失的队列任务按 DB 补回队列。异常只记日志，不打断循环。"""
    try:
        recover_unfinished_workflow_runs(reset_stale_running=True)
        recover_unfinished_image_session_generation_tasks(reset_stale_running=True)
    except Exception:
        logger.exception("队列周期对账失败")


def run_queue_reconcile_loop(
    interval_seconds: float | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """周期对账循环：先等一个间隔再对账（启动时已有一次性 recover），直到 stop_event 置位。"""
    resolved_interval = interval_seconds if interval_seconds is not None else get_reconcile_interval_seconds()
    stop = stop_event or threading.Event()
    while not stop.wait(timeout=resolved_interval):
        _reconcile_queue_once()


def start_queue_reconcile_daemon() -> threading.Thread:
    """worker 启动时开启队列周期对账 daemon 线程。"""
    thread = threading.Thread(target=run_queue_reconcile_loop, name="queue-reconcile", daemon=True)
    thread.start()
    return thread


def get_storage_cleanup_interval_seconds() -> int:
    """存储清理间隔（秒）：读环境变量 MEDIA_CLEANUP_INTERVAL_SECONDS，默认 86400，非法值回退默认。"""
    raw = os.getenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS
    try:
        interval = int(raw)
    except ValueError:
        logger.warning("MEDIA_CLEANUP_INTERVAL_SECONDS 不是合法整数，使用默认值: %s", raw)
        return DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS
    return interval if interval > 0 else DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS


def _storage_cleanup_once() -> None:
    """清理一次：只删无 DB 引用的孤儿文件与过期导出（storage_cleanup 内部宁漏勿误删）。"""
    from productflow_backend.infrastructure.db.session import get_session_factory

    try:
        db = get_session_factory()()
        try:
            report = run_cleanup(db, dry_run=False)
        finally:
            db.close()
        logger.info("存储生命周期清理完成: %s", report)
    except Exception:
        logger.exception("存储周期清理失败")


def run_storage_cleanup_loop(
    interval_seconds: float | None = None,
    *,
    stop_event: threading.Event | None = None,
    initial_delay_seconds: float = DEFAULT_STORAGE_CLEANUP_INITIAL_DELAY_SECONDS,
) -> None:
    """存储清理循环：先等 initial_delay（避开启动高峰）再首次清理，之后每 interval 一次。"""
    resolved_interval = interval_seconds if interval_seconds is not None else get_storage_cleanup_interval_seconds()
    stop = stop_event or threading.Event()
    if stop.wait(timeout=initial_delay_seconds):
        return
    while not stop.is_set():
        _storage_cleanup_once()
        if stop.wait(timeout=resolved_interval):
            return


def start_storage_cleanup_daemon() -> threading.Thread:
    """worker 启动时开启存储生命周期清理 daemon 线程。"""
    thread = threading.Thread(target=run_storage_cleanup_loop, name="storage-cleanup", daemon=True)
    thread.start()
    return thread


if _running_under_dramatiq_cli():
    cleanup_old_logs()
    recover_unfinished_workflow_runs(reset_stale_running=True)
    recover_unfinished_image_session_generation_tasks(reset_stale_running=True)
    start_queue_reconcile_daemon()
    start_storage_cleanup_daemon()
