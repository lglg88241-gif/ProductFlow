"""用量查询接口与登录面/healthz 收口：metrics 聚合、登录限速、production fail-fast。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import _login

from productflow_backend.application.time import now_utc
from productflow_backend.config import get_settings, invalidate_runtime_settings_cache
from productflow_backend.infrastructure.db.models import AgentMessage, AgentSession


@pytest.fixture()
def authed_client(configured_env: Path) -> TestClient:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    _login(client)
    return client


def _seed_usage_data(session) -> tuple[AgentSession, AgentSession]:
    """两日内若干消息 + 一条 10 天前的旧消息（by_day 应排除、总量应包含）。"""
    now = now_utc()
    session_a = AgentSession(title="用量会话A", stage="produce")
    session_b = AgentSession(title="用量会话B", stage="produce")
    session.add_all([session_a, session_b])
    session.flush()

    messages = [
        # 会话 A：今天两轮（两条 user + 两条带 usage 的 assistant）
        AgentMessage(session_id=session_a.id, role="user", content="出图", created_at=now - timedelta(minutes=5)),
        AgentMessage(
            session_id=session_a.id,
            role="assistant",
            content="好的",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            created_at=now - timedelta(minutes=4),
        ),
        AgentMessage(session_id=session_a.id, role="user", content="再改", created_at=now - timedelta(minutes=2)),
        AgentMessage(
            session_id=session_a.id,
            role="assistant",
            content="已改",
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
            created_at=now - timedelta(minutes=1),
        ),
        # 会话 B：昨天一轮
        AgentMessage(
            session_id=session_b.id,
            role="user",
            content="昨天的问题",
            created_at=now - timedelta(days=1),
        ),
        AgentMessage(
            session_id=session_b.id,
            role="assistant",
            content="昨天的回答",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            created_at=now - timedelta(days=1) + timedelta(minutes=1),
        ),
        # 10 天前的旧数据：不计入 by_day，但计入全量 total_tokens
        AgentMessage(
            session_id=session_b.id,
            role="user",
            content="很久以前",
            created_at=now - timedelta(days=10),
        ),
        AgentMessage(
            session_id=session_b.id,
            role="assistant",
            content="更早的回答",
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            created_at=now - timedelta(days=10) + timedelta(minutes=1),
        ),
    ]
    session.add_all(messages)
    session.commit()
    return session_a, session_b


def test_metrics_summary_requires_auth(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    assert client.get("/api/metrics/summary").status_code == 401


def test_metrics_summary_aggregates_sessions_turns_and_tokens(
    configured_env: Path, authed_client: TestClient, db_session
) -> None:
    session_a, _ = _seed_usage_data(db_session)
    _ = session_a
    now = now_utc()
    today_key = (now - timedelta(minutes=4)).date().isoformat()
    yesterday_key = (now - timedelta(days=1)).date().isoformat()

    response = authed_client.get("/api/metrics/summary")
    assert response.status_code == 200, response.text
    payload = response.json()

    # 全量口径：2 个会话；4 条 user 消息 = 4 轮；token 含 10 天前旧数据
    assert payload["total_sessions"] == 2
    assert payload["total_turns"] == 4
    assert payload["total_tokens"] == 150 + 30 + 15 + 1500

    # by_day：最近 7 天（含当天），缺失日补零
    by_day = payload["by_day"]
    assert len(by_day) == 7
    by_key = {item["date"]: item for item in by_day}
    assert by_key[today_key] == {
        "date": today_key,
        "prompt_tokens": 120,
        "completion_tokens": 60,
        "turns": 2,
    }
    assert by_key[yesterday_key] == {
        "date": yesterday_key,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "turns": 1,
    }
    zero_days = [item for item in by_day if item["date"] not in {today_key, yesterday_key}]
    assert all(
        item["prompt_tokens"] == 0 and item["completion_tokens"] == 0 and item["turns"] == 0 for item in zero_days
    )
    # 10 天前的数据不应出现在 by_day
    old_key = (now - timedelta(days=10)).date().isoformat()
    assert old_key not in by_key


def test_login_rate_limit_returns_429_after_repeated_failures(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app
    from productflow_backend.presentation.routes import auth as auth_module

    client = TestClient(create_app())
    try:
        for _ in range(5):
            assert client.post("/api/auth/session", json={"admin_key": "definitely-wrong"}).status_code == 401
        limited = client.post("/api/auth/session", json={"admin_key": "definitely-wrong"})
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1
        # 达到限速后即使密钥正确也被拒（窗口锁定）
        assert client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"}).status_code == 429
    finally:
        with auth_module._login_failure_lock:
            auth_module._login_failures.clear()


def test_login_success_resets_failure_counter(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app
    from productflow_backend.presentation.routes import auth as auth_module

    client = TestClient(create_app())
    try:
        # 两次失败后成功登录 → 计数清零
        assert client.post("/api/auth/session", json={"admin_key": "wrong-one"}).status_code == 401
        assert client.post("/api/auth/session", json={"admin_key": "wrong-two"}).status_code == 401
        assert client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"}).status_code == 200

        # 清零后可再承受 5 次失败，第 6 次才限速
        for _ in range(5):
            assert client.post("/api/auth/session", json={"admin_key": "wrong-again"}).status_code == 401
        assert client.post("/api/auth/session", json={"admin_key": "wrong-again"}).status_code == 429
    finally:
        with auth_module._login_failure_lock:
            auth_module._login_failures.clear()


def test_production_startup_fails_fast_when_admin_gate_disabled(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    invalidate_runtime_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="production"):
            with TestClient(create_app()):
                pass  # lifespan 启动即应抛 RuntimeError
    finally:
        get_settings.cache_clear()
        invalidate_runtime_settings_cache()


def test_production_startup_allows_admin_gate_enabled(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    invalidate_runtime_settings_cache()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200
    finally:
        get_settings.cache_clear()
        invalidate_runtime_settings_cache()


def test_healthz_provider_summary_hides_topology(configured_env: Path) -> None:
    """healthz 供应商摘要不再暴露 host/base_url；降级仅保留存在性布尔。"""
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    payload = client.get("/healthz").json()
    providers = payload["providers"]
    assert set(providers) == {"agent", "image"}
    serialized = json.dumps(providers)
    assert "host" not in serialized
    assert "base_url" not in serialized
    for purpose, summary in providers.items():
        if "error" in summary:
            continue
        expected_keys = {"kind", "model", "has_key"}
        if purpose == "agent":
            expected_keys.add("has_fallback")
        assert set(summary) == expected_keys
        assert isinstance(summary["has_key"], bool)
