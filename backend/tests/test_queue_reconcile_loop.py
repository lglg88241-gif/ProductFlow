"""队列周期对账线程测试：运行中 Redis 丢消息/滞留时由 DB（authoritative state）兜底补发。

`recover` 此前只在进程启动时跑一次；对账 daemon 线程按 RECONCILE_INTERVAL_SECONDS（默认 1800）
周期调用，异常只记日志、不影响 dramatiq actor 注册。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


def test_reconcile_once_calls_both_recover_functions(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import productflow_backend.workers as workers_module

    calls: list[str] = []
    monkeypatch.setattr(
        workers_module,
        "recover_unfinished_workflow_runs",
        lambda **kwargs: calls.append("workflow"),
    )
    monkeypatch.setattr(
        workers_module,
        "recover_unfinished_image_session_generation_tasks",
        lambda **kwargs: calls.append("image_session"),
    )

    workers_module._reconcile_queue_once()

    assert calls == ["workflow", "image_session"]


def test_reconcile_once_swallows_exceptions(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对账失败只记日志，不向上抛出（周期循环不能被单次异常打断）。"""
    import productflow_backend.workers as workers_module

    def _boom(**kwargs):
        raise RuntimeError("redis 不可用")

    monkeypatch.setattr(workers_module, "recover_unfinished_workflow_runs", _boom)

    workers_module._reconcile_queue_once()  # 不应抛出


def test_reconcile_loop_runs_periodically_until_stopped(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import productflow_backend.workers as workers_module

    counter = {"reconciles": 0}
    enough = threading.Event()

    def _fake_reconcile() -> None:
        counter["reconciles"] += 1
        if counter["reconciles"] >= 2:
            enough.set()

    monkeypatch.setattr(workers_module, "_reconcile_queue_once", _fake_reconcile)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=workers_module.run_queue_reconcile_loop,
        kwargs={"interval_seconds": 0.02, "stop_event": stop_event},
        daemon=True,
    )
    thread.start()
    try:
        assert enough.wait(timeout=5), "对账循环应周期执行"
    finally:
        stop_event.set()
        thread.join(timeout=5)

    assert counter["reconciles"] >= 2
    assert not thread.is_alive()


def test_reconcile_interval_reads_env(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import productflow_backend.workers as workers_module

    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "60")
    assert workers_module.get_reconcile_interval_seconds() == 60

    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "not-a-number")
    assert workers_module.get_reconcile_interval_seconds() == 1800

    monkeypatch.delenv("RECONCILE_INTERVAL_SECONDS", raising=False)
    assert workers_module.get_reconcile_interval_seconds() == 1800
