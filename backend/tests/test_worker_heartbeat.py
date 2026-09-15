"""审计 O3：worker 心跳（Redis 键）+ 诊断端点 worker 心跳字段。

- workers.write_worker_heartbeat：可注入 redis 客户端，写 {"ts","pid"} + TTL，失败只返回 False；
- workers.run_worker_heartbeat_loop / start_worker_heartbeat_daemon：先写后等、失败重试；
- /api/admin/diagnostics 的 worker_heartbeat_age_seconds / worker_heartbeat_alive 两态。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# workers 在导入期就 configure_logging() → 读环境，必须等 configured_env 生效后在函数内导入
def _load_workers():
    from productflow_backend import workers

    return workers


class _FakeRedis:
    """记录 set/get 调用的最小 fake；fail_set 次数内让 set 抛错，模拟 Redis 抖动。"""

    def __init__(self, *, fail_set_times: int = 0) -> None:
        self.data: dict[str, tuple[object, object]] = {}
        self.fail_set_times = fail_set_times
        self.set_calls = 0

    def set(self, key: str, value: object, ex: object = None) -> bool:
        self.set_calls += 1
        if self.fail_set_times > 0:
            self.fail_set_times -= 1
            raise ConnectionError("redis down")
        self.data[key] = (value, ex)
        return True

    def get(self, key: str) -> object:
        item = self.data.get(key)
        return item[0] if item is not None else None


def test_write_worker_heartbeat_sets_key_with_ttl_and_payload(configured_env: Path) -> None:
    workers = _load_workers()
    fake = _FakeRedis()
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

    assert workers.write_worker_heartbeat(fake, now=now) is True

    value, ex = fake.data[workers.WORKER_HEARTBEAT_KEY]
    assert ex == workers.WORKER_HEARTBEAT_TTL_SECONDS == 120
    payload = json.loads(value)  # type: ignore[arg-type]
    assert payload["pid"] == os.getpid()
    assert datetime.fromisoformat(payload["ts"]) == now


def test_write_worker_heartbeat_swallows_redis_failure(configured_env: Path) -> None:
    workers = _load_workers()
    fake = _FakeRedis(fail_set_times=1)

    # Redis 写失败：返回 False、不抛异常、不残留键
    assert workers.write_worker_heartbeat(fake) is False
    assert workers.WORKER_HEARTBEAT_KEY not in fake.data


def test_write_worker_heartbeat_builds_own_client_when_not_injected(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _load_workers()
    fake = _FakeRedis()
    monkeypatch.setattr(workers, "_new_heartbeat_redis_client", lambda: fake)

    assert workers.write_worker_heartbeat() is True
    assert workers.WORKER_HEARTBEAT_KEY in fake.data


def test_worker_heartbeat_loop_writes_immediately_then_retries(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _load_workers()
    fake = _FakeRedis(fail_set_times=2)  # 首两次失败，第三次成功：循环不得被打断
    monkeypatch.setattr(workers, "_new_heartbeat_redis_client_safely", lambda: fake)
    stop = threading.Event()
    stop.wait = lambda timeout=None: fake.set_calls >= 3  # type: ignore[method-assign]

    workers.run_worker_heartbeat_loop(interval_seconds=0.01, stop_event=stop)
    assert fake.set_calls == 3
    assert workers.WORKER_HEARTBEAT_KEY in fake.data


def test_worker_heartbeat_loop_stops_without_writing_when_stopped_before_start(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _load_workers()
    fake = _FakeRedis()
    monkeypatch.setattr(workers, "_new_heartbeat_redis_client_safely", lambda: fake)
    stop = threading.Event()
    stop.set()

    workers.run_worker_heartbeat_loop(interval_seconds=0.01, stop_event=stop)
    assert fake.set_calls == 0


def test_start_worker_heartbeat_daemon_runs_in_background(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _load_workers()
    fake = _FakeRedis()
    monkeypatch.setattr(workers, "_new_heartbeat_redis_client_safely", lambda: fake)
    stop = threading.Event()
    # 写入一次后立即叫停循环，避免后台线程残留
    stop.wait = lambda timeout=None: fake.set_calls >= 1  # type: ignore[method-assign]

    thread = workers.start_worker_heartbeat_daemon(fake, stop_event=stop)
    assert thread.daemon is True
    assert thread.name == "worker-heartbeat"
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert fake.set_calls >= 1


def test_diagnostics_reports_fresh_heartbeat_alive(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import _login

    from productflow_backend.presentation.api import create_app

    workers = _load_workers()
    heartbeat_value = json.dumps({"ts": datetime.now(UTC).isoformat(), "pid": 12345}).encode()

    class _FakeDiagnosticsRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> bytes | None:
            return heartbeat_value if key == workers.WORKER_HEARTBEAT_KEY else None

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _FakeDiagnosticsRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    payload = client.get("/api/admin/diagnostics").json()
    age = payload["worker_heartbeat_age_seconds"]
    assert isinstance(age, float) and 0 <= age < workers.WORKER_HEARTBEAT_TTL_SECONDS
    assert payload["worker_heartbeat_alive"] is True


def test_diagnostics_reports_missing_heartbeat_dead(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import _login

    from productflow_backend.presentation.api import create_app

    class _EmptyRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> None:
            return None

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _EmptyRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    payload = client.get("/api/admin/diagnostics").json()
    assert payload["worker_heartbeat_age_seconds"] is None
    assert payload["worker_heartbeat_alive"] is False


def test_diagnostics_tolerates_corrupted_heartbeat_payload(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helpers import _login

    from productflow_backend.presentation.api import create_app

    class _GarbageRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> bytes:
            return b"not-json"

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _GarbageRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    payload = client.get("/api/admin/diagnostics").json()
    assert payload["worker_heartbeat_age_seconds"] is None
    assert payload["worker_heartbeat_alive"] is False


def test_stale_heartbeat_beyond_ttl_is_not_alive(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """键存在但 ts 已超过 TTL（理论上 TTL 已让它消失，防御时钟/人为写入）：alive=False。"""
    from helpers import _login

    from productflow_backend.presentation.api import create_app

    workers = _load_workers()
    stale_ts = (datetime.now(UTC) - timedelta(seconds=workers.WORKER_HEARTBEAT_TTL_SECONDS + 30)).isoformat()
    heartbeat_value = json.dumps({"ts": stale_ts, "pid": 1}).encode()

    class _StaleRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> bytes | None:
            return heartbeat_value if key == workers.WORKER_HEARTBEAT_KEY else None

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _StaleRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    payload = client.get("/api/admin/diagnostics").json()
    assert payload["worker_heartbeat_alive"] is False
