"""A2 审计批次：healthz 最小存活探针 + 管理员诊断端点 + 门禁检查前置。

- /healthz 只回答进程存活，不再暴露 admin_access_required / providers（侦察面收敛）。
- 部署细节（providers 摘要、db/redis 连通性、alembic 版本、app 版本）
  收敛到 GET /api/admin/diagnostics（require_admin）。
- production 关门禁的拒绝必须发生在 create_app() 内（构建 app 之前），
  而不是 lifespan 启动时。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import _login


def test_healthz_is_minimal_liveness_probe(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_diagnostics_requires_auth(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    response = client.get("/api/admin/diagnostics")
    assert response.status_code == 401


def test_admin_diagnostics_reports_each_probe_independently(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """诊断端点各子项独立容错：redis 探测失败只降级该子项，结构完整、不 500。"""
    from productflow_backend import __version__
    from productflow_backend.presentation.api import create_app

    class _BrokenRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            raise ConnectionError("redis down")

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _BrokenRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    response = client.get("/api/admin/diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "app_version",
        "providers",
        "db_reachable",
        "redis_reachable",
        "alembic_version",
        # 审计 O3：worker 心跳字段（Redis 故障 → 年龄 null / 存活 False）
        "worker_heartbeat_age_seconds",
        "worker_heartbeat_alive",
    }
    assert payload["app_version"] == __version__
    assert payload["db_reachable"] is True
    assert payload["redis_reachable"] is False
    assert payload["worker_heartbeat_age_seconds"] is None
    assert payload["worker_heartbeat_alive"] is False
    # 测试库用 Base.metadata.create_all 建表，没有 alembic_version → null
    assert payload["alembic_version"] is None
    providers = payload["providers"]
    assert set(providers) == {"agent", "image"}
    assert providers["agent"]["kind"] == "mock"
    assert providers["image"]["kind"] == "mock"


def test_admin_diagnostics_redis_reachable_reflects_probe_result(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redis ping 成功时子项为 True（探测结果真实反映连通性，不写死）。"""
    from productflow_backend.presentation.api import create_app

    class _FakeRedis:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            return True

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.admin_diagnostics.redis_lib.Redis.from_url",
        classmethod(lambda cls, url, **kwargs: _FakeRedis()),
    )
    client = TestClient(create_app())
    _login(client)
    assert client.get("/api/admin/diagnostics").json()["redis_reachable"] is True


def test_admin_diagnostics_alembic_version_read_when_table_exists(configured_env: Path) -> None:
    """alembic_version 表存在时诊断端点应读出 version_num。"""
    from sqlalchemy import text

    from productflow_backend.infrastructure.db.session import get_engine
    from productflow_backend.presentation.api import create_app

    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0007_audit')"))

    client = TestClient(create_app())
    _login(client)
    payload = client.get("/api/admin/diagnostics").json()
    assert payload["alembic_version"] == "0007_audit"


def test_production_gate_check_fails_at_create_app_time(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """production + 关门禁必须在 create_app() 内立刻抛 RuntimeError（门禁检查前置），
    而不是等 lifespan/TestClient 上下文进入时才失败。"""
    from productflow_backend.config import get_settings, invalidate_runtime_settings_cache
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATA_ISOLATION_ENABLED", "true")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    invalidate_runtime_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="production"):
            create_app()
    finally:
        get_settings.cache_clear()
        invalidate_runtime_settings_cache()


def test_development_gate_disabled_warns_but_app_builds(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """development + 关门禁：create_app 正常完成，仅告警。"""
    import logging

    from productflow_backend.config import get_settings, invalidate_runtime_settings_cache
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    invalidate_runtime_settings_cache()
    try:
        with caplog.at_level(logging.WARNING):
            app = create_app()
        assert any("管理员访问密钥已关闭" in record.getMessage() for record in caplog.records)
        client = TestClient(app)
        assert client.get("/healthz").json() == {"status": "ok"}
    finally:
        get_settings.cache_clear()
        invalidate_runtime_settings_cache()
