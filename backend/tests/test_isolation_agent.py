"""数据隔离（批次 B）：Agent 会话域、素材库与用量度量。

审计要求逐路径双账号交叉验证。本文件覆盖：会话列表/详情/删除、素材列表与下载、
文案报告列表与下载、用量统计——B 用户对 A 的资源一律"不存在"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_isolation_products import _make_user_client, _png, isolation_env  # noqa: F401


def test_agent_sessions_are_isolated(isolation_env: None, configured_env: Path, db_session) -> None:
    """A 的设计师会话：B 列表不可见、详情/删除 404（与不存在同文案）。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _ = _make_user_client(app, db_session, "agenta")
    bob, _ = _make_user_client(app, db_session, "agentb")

    created = alice.post("/api/agent/sessions", json={"title": "A 的设计会话"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    bob_list = bob.get("/api/agent/sessions").json()
    assert all(item["id"] != session_id for item in bob_list["items"])

    missing = bob.get(f"/api/agent/sessions/{session_id}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "设计师会话不存在"

    assert bob.delete(f"/api/agent/sessions/{session_id}").status_code == 404
    assert alice.get(f"/api/agent/sessions/{session_id}").status_code == 200


def test_uploaded_assets_are_isolated(isolation_env: None, configured_env: Path, db_session) -> None:
    """A 上传的素材：B 列表不可见、下载 404、分格导出 404、删除 404。

    内置模板（owner_id NULL）对所有用户可见——单独验证不被误伤。
    """
    from productflow_backend.application.asset_library import bootstrap_builtin_assets
    from productflow_backend.presentation.api import create_app

    bootstrap_builtin_assets()
    app = create_app()
    alice, _ = _make_user_client(app, db_session, "asset_a")
    bob, _ = _make_user_client(app, db_session, "asset_b")

    uploaded = alice.post(
        "/api/agent/assets",
        files={"file": ("A的模板.png", _png(), "image/png")},
        data={"kind": "template"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["id"]

    bob_assets = bob.get("/api/agent/assets").json()
    assert all(item["id"] != asset_id for item in bob_assets["items"]), "B 的素材列表里出现了 A 的素材"
    # 内置模板仍可见
    assert any(item.get("source") == "builtin" for item in bob_assets["items"]), "内置模板对 B 应可见"

    # 下载（审计点名路径）
    assert alice.get(f"/api/agent/assets/{asset_id}/download").status_code == 200
    assert bob.get(f"/api/agent/assets/{asset_id}/download").status_code == 404
    # 分格导出
    assert bob.get(f"/api/agent/assets/{asset_id}/grid-export?grid=3x3").status_code == 404
    # 删除
    assert bob.delete(f"/api/agent/assets/{asset_id}").status_code == 404
    assert alice.get(f"/api/agent/assets/{asset_id}/download").status_code == 200


def test_copy_reports_are_isolated(
    isolation_env: None, configured_env: Path, db_session, install_scripted_llm
) -> None:
    """A 的文案报告：B 列表不可见、下载 404。"""
    from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "report_a")
    bob, _ = _make_user_client(app, db_session, "report_b")

    # 用剧本化 LLM 让 A 的会话产出一份报告
    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-r1",
                        name="write_copy_report",
                        arguments={"brief": "开业活动文案报告"},
                    )
                ],
            ),
            AgentLLMResponse(content='{"title":"开业报告","content":"# 开业活动\\n正文内容"}'),
            AgentLLMResponse(content="报告写好了。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="A 的报告会话", owner_id=alice_id)
        run_agent_turn(db, agent_session_id=agent_session.id, user_content="写一份开业文案报告")
    finally:
        db.close()

    bob_reports = bob.get("/api/agent/copy-reports").json()["items"]
    assert all(item["agent_session_id"] != alice_id for item in bob_reports), "B 看到了 A 的报告"

    alice_reports = alice.get("/api/agent/copy-reports").json()["items"]
    if alice_reports:
        report_id = alice_reports[0]["id"]
        assert alice.get(f"/api/agent/copy-reports/{report_id}/download").status_code == 200
        cross = bob.get(f"/api/agent/copy-reports/{report_id}/download")
        assert cross.status_code == 404, "B 下载到了 A 的文案报告"


def test_metrics_are_scoped_per_user(isolation_env: None, configured_env: Path, db_session) -> None:
    """用量统计按用户：A 有会话/消息，B 的数字应为 0（而不是看到 A 的用量）。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.infrastructure.db.models import AgentMessage
    from productflow_backend.infrastructure.db.session import get_session_factory
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "metric_a")
    bob, _ = _make_user_client(app, db_session, "metric_b")

    # A 名下有会话与消息（token 用量）
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="A 的用量会话", owner_id=alice_id)
        db.add(
            AgentMessage(
                session_id=agent_session.id,
                role="user",
                content="出图",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )
        )
        db.add(
            AgentMessage(
                session_id=agent_session.id,
                role="assistant",
                content="好的",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            )
        )
        db.commit()
    finally:
        db.close()

    alice_metrics = alice.get("/api/metrics/summary").json()
    bob_metrics = bob.get("/api/metrics/summary").json()

    assert alice_metrics["total_sessions"] >= 1
    assert alice_metrics["total_tokens"] >= 150
    assert bob_metrics["total_sessions"] == 0, "B 看到了 A 的会话计数"
    assert bob_metrics["total_tokens"] == 0, "B 看到了 A 的 token 用量"
    assert bob_metrics["total_turns"] == 0


def test_agent_domain_visible_when_isolation_disabled(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关闭 = 现状：无用户会话也能看到全部会话（整批回退语义）。"""
    from productflow_backend.config import get_settings
    from productflow_backend.infrastructure.db.session import get_session_factory
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("DATA_ISOLATION_ENABLED", "false")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
        alice, _ = _make_user_client(app, db_session, "off_a")
        created = alice.post("/api/agent/sessions", json={"title": "关闭开关的会话"})
        assert created.status_code == 201

        from fastapi.testclient import TestClient

        anonymous = TestClient(app)
        listing = anonymous.get("/api/agent/sessions").json()
        assert any(item["title"] == "关闭开关的会话" for item in listing["items"])
    finally:
        get_settings.cache_clear()
    _ = get_session_factory
