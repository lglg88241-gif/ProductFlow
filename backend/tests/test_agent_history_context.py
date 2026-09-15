"""短历史上下文完整性（审计 R4）：历史裁剪不得丢掉工具调用之前已确认的需求。

复现的问题：`_build_llm_messages` 把起点设为"第一个带 tool_calls 的 assistant"，
于是该 assistant 之前的消息（用户最初的需求）被整体丢弃——只有 4 条消息的短会话
也会丢需求，用户被迫重述。

本文件与修复一一对应，可独立回退。
"""

from __future__ import annotations

import json
from pathlib import Path


def _assistant_tool_calls(*calls: tuple[str, str]) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls_json": [{"call_id": call_id, "name": name, "arguments": {}} for call_id, name in calls],
    }


def test_short_history_keeps_requirement_before_tool_calls(configured_env: Path) -> None:
    """4 条消息的短会话：首个工具调用之前的需求必须保留在上下文里。"""
    from productflow_backend.application.designer_agent.loop import (
        _build_llm_messages,
        create_agent_session,
        get_agent_session,
    )
    from productflow_backend.infrastructure.db.models import AgentMessage
    from productflow_backend.infrastructure.db.session import get_session_factory

    requirement = "我要给护手霜做朋友圈海报，价格 39.9，主打保湿"
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="短历史上下文")
        db.add_all(
            [
                AgentMessage(session_id=agent_session.id, role="user", content=requirement),
                AgentMessage(
                    session_id=agent_session.id,
                    **_assistant_tool_calls(("call-1", "generate_image")),
                ),
                AgentMessage(
                    session_id=agent_session.id,
                    role="tool",
                    content=json.dumps({"status": "completed"}),
                    tool_call_id="call-1",
                    tool_name="generate_image",
                ),
                AgentMessage(session_id=agent_session.id, role="assistant", content="海报出好了。"),
            ]
        )
        db.commit()
        db.expire_all()
        refreshed = get_agent_session(db, agent_session.id)

        built = _build_llm_messages(refreshed)
        contents = [str(m.get("content", "")) for m in built]

        assert any(requirement in content for content in contents), (
            "工具调用之前的需求被裁掉了——模型会以为用户什么都没说。"
            f"实际上下文: {contents}"
        )
    finally:
        db.close()


def test_truncation_still_drops_unpaired_tool_groups(configured_env: Path) -> None:
    """保留需求的同时，残缺工具组（断线产物）仍必须被剔除，否则 API 会 400。"""
    from productflow_backend.application.designer_agent.loop import (
        _build_llm_messages,
        create_agent_session,
        get_agent_session,
    )
    from productflow_backend.infrastructure.db.models import AgentMessage
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="残缺组")
        db.add_all(
            [
                AgentMessage(session_id=agent_session.id, role="user", content="原始需求：开业海报"),
                # 断线产物：assistant 的 tool_calls 已落库，tool 结果缺失
                AgentMessage(
                    session_id=agent_session.id,
                    **_assistant_tool_calls(("call-orphan", "write_copy")),
                ),
                AgentMessage(session_id=agent_session.id, role="user", content="在吗？"),
            ]
        )
        db.commit()
        db.expire_all()
        refreshed = get_agent_session(db, agent_session.id)

        built = _build_llm_messages(refreshed)
        call_ids = {
            call["id"] for message in built for call in (message.get("tool_calls") or [])
        }
        assert "call-orphan" not in call_ids, "残缺工具组必须剔除"
        contents = [str(m.get("content", "")) for m in built]
        assert any("原始需求：开业海报" in content for content in contents), "需求仍须保留"
    finally:
        db.close()


def test_tool_result_without_its_assistant_group_is_dropped(configured_env: Path) -> None:
    """窗口截断把 assistant 切掉、只剩 tool 结果时，该 tool 消息必须丢弃（否则 400）。"""
    from productflow_backend.application.designer_agent.loop import (
        _build_llm_messages,
        create_agent_session,
        get_agent_session,
    )
    from productflow_backend.infrastructure.db.models import AgentMessage
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="孤立 tool 结果")
        db.add_all(
            [
                # 没有配对的 assistant(tool_calls)，属于截断产物
                AgentMessage(
                    session_id=agent_session.id,
                    role="tool",
                    content=json.dumps({"status": "completed"}),
                    tool_call_id="call-lost",
                    tool_name="write_copy",
                ),
                AgentMessage(session_id=agent_session.id, role="user", content="继续"),
            ]
        )
        db.commit()
        db.expire_all()
        refreshed = get_agent_session(db, agent_session.id)

        built = _build_llm_messages(refreshed)
        assert not [m for m in built if m.get("role") == "tool"], "孤立 tool 结果必须丢弃"
        assert any(str(m.get("content", "")) == "继续" for m in built)
    finally:
        db.close()
