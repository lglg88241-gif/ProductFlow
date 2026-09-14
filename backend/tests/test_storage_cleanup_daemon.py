"""存储生命周期清理 daemon：间隔读取、initial_delay、循环体执行与停止语义。"""

from __future__ import annotations

import threading


# workers 在导入期就 configure_logging() → 读环境，必须等 configured_env 生效后在函数内导入
def _load_workers():
    from productflow_backend import workers

    return workers


def test_storage_cleanup_interval_reads_env(configured_env, monkeypatch) -> None:
    workers = _load_workers()
    monkeypatch.delenv("MEDIA_CLEANUP_INTERVAL_SECONDS", raising=False)
    assert workers.get_storage_cleanup_interval_seconds() == workers.DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS
    monkeypatch.setenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "3600")
    assert workers.get_storage_cleanup_interval_seconds() == 3600
    monkeypatch.setenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "abc")
    assert workers.get_storage_cleanup_interval_seconds() == workers.DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS
    monkeypatch.setenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "0")
    assert workers.get_storage_cleanup_interval_seconds() == workers.DEFAULT_STORAGE_CLEANUP_INTERVAL_SECONDS


def test_storage_cleanup_loop_runs_and_stops(configured_env, monkeypatch) -> None:
    workers = _load_workers()
    calls: list[int] = []
    monkeypatch.setattr(workers, "_storage_cleanup_once", lambda: calls.append(1))
    stop = threading.Event()

    def stop_after_first_wait(timeout: float | None = None) -> bool:
        if calls:
            stop.set()
            return True
        return False

    stop.wait = stop_after_first_wait  # type: ignore[method-assign]
    workers.run_storage_cleanup_loop(interval_seconds=0.01, stop_event=stop, initial_delay_seconds=0)
    assert len(calls) >= 1


def test_storage_cleanup_loop_respects_initial_delay(configured_env, monkeypatch) -> None:
    workers = _load_workers()
    calls: list[int] = []
    monkeypatch.setattr(workers, "_storage_cleanup_once", lambda: calls.append(1))
    stop = threading.Event()
    # initial_delay 期间被 stop 唤醒 → 一次都不应执行
    stop.set()
    workers.run_storage_cleanup_loop(interval_seconds=0.01, stop_event=stop, initial_delay_seconds=0.01)
    assert calls == []


def test_storage_cleanup_once_reports_and_closes_session(configured_env, monkeypatch) -> None:
    workers = _load_workers()
    report = {"scanned": 3, "deletable": 1, "freed_bytes": 10}
    closed: list[bool] = []

    class _FakeDB:
        def close(self) -> None:
            closed.append(True)

    class _FakeFactory:
        def __call__(self) -> _FakeDB:
            return _FakeDB()

    import productflow_backend.infrastructure.db.session as db_session

    monkeypatch.setattr(db_session, "get_session_factory", lambda: _FakeFactory())
    seen: dict[str, object] = {}

    def _fake_run_cleanup(db, *, dry_run: bool) -> dict[str, object]:
        seen["dry_run"] = dry_run
        return report

    # workers 在模块顶层导入了 run_cleanup，patch 必须落在 workers 命名空间
    monkeypatch.setattr(workers, "run_cleanup", _fake_run_cleanup)
    workers._storage_cleanup_once()
    assert seen["dry_run"] is False
    assert closed == [True]
