"""设计师 Agent 的编排循环与路由测试（spec §10 剧本的确定性回放）。

`install_scripted_llm` fixture 由 conftest 提供。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _enable_deletion, _login

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall


def _copy_script() -> list[AgentLLMResponse]:
    return [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-copy-1",
                    name="write_copy",
                    arguments={"brief": "美容院周末开业酬宾，面部护理 199 元体验，语气亲切", "kind": "moments"},
                )
            ],
        ),
        AgentLLMResponse(
            content=json.dumps(
                [
                    {"title": "开业大酬宾", "content": "本周末美容院开业，面部护理体验价 199 元", "hashtags": ["开业"]},
                    {"title": "周末盛典", "content": "周末来做一次面部 SPA，开业价只在两天", "hashtags": ["美容"]},
                    {"title": "美丽开业", "content": "新店开业，前 50 名到店有礼", "hashtags": ["新店"]},
                ],
                ensure_ascii=False,
            )
        ),
        AgentLLMResponse(content="给您准备了 3 版朋友圈文案，挑一版喜欢的，我就开始出图。"),
    ]


def test_agent_turn_writes_copy_through_tool_script(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(_copy_script())
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="开业海报")
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="我要周末做个开业酬宾的朋友圈海报")

        roles = [message.role for message in result.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert agent_session.stage == "produce"

        tool_message = result.messages[2]
        assert tool_message.tool_name == "write_copy"
        tool_result = json.loads(tool_message.content)
        assert tool_result["status"] == "completed"
        assert len(tool_result["copies"]) == 3

        final = result.messages[3]
        assert "3 版" in final.content
        # system prompt 在每轮都排在最前
        assert install_scripted_llm and result.tool_events[0]["tool"] == "write_copy"
    finally:
        db.close()


def test_agent_turn_generates_image_and_links_session(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    script = [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-image-1",
                    name="generate_image",
                    arguments={
                        "prompt": "美容院开业海报，粉色系，面部护理主视觉，文案位在下方",
                        "size": "1080x1440",
                    },
                )
            ],
        ),
        AgentLLMResponse(content="图片已经生成好了！可以让我换配色、加价格或换尺寸。"),
    ]
    install_scripted_llm(script)
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="出图")
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="就要刚才第二版文案，出图吧")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "completed", tool_result
        # 尺寸按生成约束归一到 16 的倍数
        assert tool_result["size"] == "1072x1440"
        assert len(tool_result["completed_assets"]) == 1

        assert agent_session.stage == "review"
        assert agent_session.image_session_id is not None
        assert result.pending_generation_tasks == []

        download_url = tool_result["completed_assets"][0]["download_url"]
        assert download_url.startswith("/api/image-session-assets/")
    finally:
        db.close()


def test_agent_tool_failure_is_translated_for_the_user(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    script = [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-image-2",
                    name="generate_image",
                    arguments={"prompt": "   ", "size": "1024x1024"},
                )
            ],
        ),
        AgentLLMResponse(content="画图前我需要再确认一下画面内容，你想突出什么主体？"),
    ]
    install_scripted_llm(script)
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="随便来张图")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "error"
        assert "画什么" in tool_result["message"]
        assert result.messages[3].role == "assistant"
    finally:
        db.close()


def test_agent_routes_require_login_and_toggle_delete(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.presentation.api import create_app

    install_scripted_llm(_copy_script())
    app = create_app()
    client = TestClient(app)

    assert client.get("/api/agent/sessions").status_code == 401
    _login(client)

    created = client.post("/api/agent/sessions", json={"title": "路由测试"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["stage"] == "clarify"

    listed = client.get("/api/agent/sessions")
    assert listed.status_code == 200
    assert any(item["id"] == session_id for item in listed.json()["items"])

    turned = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "我要周末做个开业酬宾的朋友圈海报"},
    )
    assert turned.status_code == 200
    payload = turned.json()
    assert [message["role"] for message in payload["session"]["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert payload["session"]["stage"] == "produce"

    detail = client.get(f"/api/agent/sessions/{session_id}")
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 4

    missing = client.post(f"/api/agent/sessions/{session_id}/messages", json={"content": "   "})
    assert missing.status_code == 400

    not_found = client.post("/api/agent/sessions/no-such-session/messages", json={"content": "在吗"})
    assert not_found.status_code == 404

    assert client.delete(f"/api/agent/sessions/{session_id}").status_code == 403
    _enable_deletion(client)
    assert client.delete(f"/api/agent/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/agent/sessions/{session_id}").status_code == 404


def test_agent_reports_missing_real_text_provider(configured_env: Path) -> None:
    from productflow_backend.application.designer_agent.loop import is_agent_llm_available

    available, message = is_agent_llm_available()
    assert available is False
    assert "AGENT_*" in message
