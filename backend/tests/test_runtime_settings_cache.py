from __future__ import annotations

from pathlib import Path

import pytest

from productflow_backend import config
from productflow_backend.config import (
    get_runtime_settings,
    invalidate_runtime_settings_cache,
)
from productflow_backend.infrastructure.db.models import AppSetting
from productflow_backend.infrastructure.db.session import get_session_factory


def test_runtime_settings_cache_reuses_reads_until_invalidated(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override_queries = 0
    original_load = config._load_database_config_overrides

    def counting_load() -> dict[str, str]:
        nonlocal override_queries
        override_queries += 1
        return original_load()

    monkeypatch.setattr(config, "_load_database_config_overrides", counting_load)

    invalidate_runtime_settings_cache()
    first = get_runtime_settings()
    second = get_runtime_settings()

    assert override_queries == 1
    assert second is first

    invalidate_runtime_settings_cache()
    session = get_session_factory()()
    try:
        session.add(AppSetting(key="generation_max_concurrent_tasks", value="7"))
        session.commit()
    finally:
        session.close()
    third = get_runtime_settings()

    assert override_queries == 2
    assert third is not first
    assert third.generation_max_concurrent_tasks == 7


def test_direct_database_seed_invalidates_cache_automatically(configured_env: Path) -> None:
    assert get_runtime_settings().generation_max_concurrent_tasks == 3

    session = get_session_factory()()
    try:
        session.add(AppSetting(key="generation_max_concurrent_tasks", value="7"))
        session.commit()
    finally:
        session.close()

    # No explicit invalidation: the ORM commit listener must handle it.
    assert get_runtime_settings().generation_max_concurrent_tasks == 7


def test_config_update_is_visible_immediately_despite_cache(configured_env: Path) -> None:
    from fastapi.testclient import TestClient

    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    assert client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"}).status_code == 200
    assert client.post("/api/settings/unlock", json={"token": "super-secret-settings-token"}).status_code == 200

    assert get_runtime_settings().deletion_enabled is False

    updated = client.patch(
        "/api/settings",
        json={"values": {"deletion_enabled": "true"}, "reset_keys": []},
    )
    assert updated.status_code == 200
    assert get_runtime_settings().deletion_enabled is True

    reset = client.patch("/api/settings", json={"values": {}, "reset_keys": ["deletion_enabled"]})
    assert reset.status_code == 200
    assert get_runtime_settings().deletion_enabled is False
