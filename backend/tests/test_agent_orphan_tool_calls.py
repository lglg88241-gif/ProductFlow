"""孤儿 tool_calls 自愈测试：SSE 断线可能留下 assistant(tool_calls) 已落库、tool 结果未落库的半截历史。

OpenAI 兼容 API 对无配对结果的 tool_calls 会直接 400，且该会话此后每轮复现、无自愈。
`_build_llm_messages` 构建时校验配对：含无配对结果 call 的 assistant 组整组跳过（无状态过滤、不改库）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall
from productflow_backend.infrastructure.db.models import AgentMessage


def _insert_history(db, agent_session, entries: list[dict]) -> None:
    """按给定顺序手工落库消息（显式递增 created_at 保证排序确定性，且整体在过去）。"""
    base = datetime.now(UTC) - timedelta(hours=1)
    for index, entry in enumerate(entries):
        db.add(
            AgentMessage(
                session_id=agent_session.id,
                role=entry["role"],
                content=entry.get("content", ""),
                tool_calls_json=entry.get("tool_calls_json"),
                tool_call_id=entry.get("tool_call_id"),
                tool_name=entry.get("tool_name"),
                created_at=base + timedelta(seconds=index + 1),
            )
        )
    db.commit()


def test_build_llm_messages_skips_orphan_tool_call_group(configured_env: Path) -> None:
    """孤儿 assistant 组不发给 LLM，正常组与其后消息保留。"""
    from productflow_backend.application.designer_agent.loop import _build_llm_messages, get_agent_session
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        from productflow_backend.application.designer_agent.loop import create_agent_session

        agent_session = create_agent_session(db, title="孤儿历史")
        _insert_history(
            db,
            agent_session,
            [
                {"role": "user", "content": "上一次的请求"},
                # 孤儿组：assistant(tool_calls) 已落库，tool 结果因断线缺失
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls_json": [
                        {"call_id": "call-orphan", "name": "write_copy", "arguments": {"brief": "开业"}}
                    ],
                },
                # 正常组：tool_calls 与结果成对
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls_json": [{"call_id": "call-ok", "name": "write_copy", "arguments": {"brief": "新开业"}}],
                },
                {"role": "tool", "content": "{}", "tool_call_id": "call-ok", "tool_name": "write_copy"},
                {"role": "assistant", "content": "最终回复"},
            ],
        )

        messages = _build_llm_messages(get_agent_session(db, agent_session.id))
        serialized = json.dumps(messages, ensure_ascii=False)

        assert "call-orphan" not in serialized, "孤儿组的 tool_calls 不应发给 LLM"
        assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call-ok" for m in messages)
        assert messages[0]["role"] == "system"
        assert messages[-1] == {"role": "assistant", "content": "最终回复"}
    finally:
        db.close()


def test_build_llm_messages_skips_group_with_partially_missing_results(configured_env: Path) -> None:
    """assistant 带多个 call 但只有部分结果时，整组（含已落库的残缺结果）都跳过。"""
    from productflow_backend.application.designer_agent.loop import (
        _build_llm_messages,
        create_agent_session,
        get_agent_session,
    )
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        _insert_history(
            db,
            agent_session,
            [
                {"role": "user", "content": "问1"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls_json": [
                        {"call_id": "call-a", "name": "write_copy", "arguments": {}},
                        {"call_id": "call-b", "name": "write_copy", "arguments": {}},
                    ],
                },
                # 只有 call-a 的结果，call-b 缺失
                {"role": "tool", "content": "{}", "tool_call_id": "call-a", "tool_name": "write_copy"},
                {"role": "assistant", "content": "后来的回复"},
            ],
        )

        messages = _build_llm_messages(get_agent_session(db, agent_session.id))
        serialized = json.dumps(messages, ensure_ascii=False)

        assert "call-a" not in serialized and "call-b" not in serialized
        assert [m["role"] for m in messages] == ["system", "assistant"]
        assert messages[-1]["content"] == "后来的回复"
    finally:
        db.close()


def test_run_agent_turn_survives_orphan_tool_calls_in_history(configured_env: Path, install_scripted_llm) -> None:
    """回归：历史里存在孤儿组时，run_agent_turn 仍能正常走完且不再把孤儿发给 LLM。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="孤儿回归")
        _insert_history(
            db,
            agent_session,
            [
                {"role": "user", "content": "上一次的请求"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls_json": [
                        {"call_id": "call-orphan", "name": "write_copy", "arguments": {"brief": "开业"}}
                    ],
                },
            ],
        )

        llm = install_scripted_llm(
            [
                AgentLLMResponse(
                    content=None,
                    tool_calls=[
                        AgentToolCall(call_id="call-new", name="write_copy", arguments={"brief": "新一轮开业文案"})
                    ],
                ),
                AgentLLMResponse(
                    content=json.dumps(
                        [{"title": "A", "content": "文案A", "hashtags": []}], ensure_ascii=False
                    )
                ),
                AgentLLMResponse(content="新文案来啦。"),
            ]
        )
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="再来一次")

        # 发给 LLM 的历史不含孤儿 call，新一轮正常完成工具循环
        assert all("call-orphan" not in json.dumps(call["messages"], ensure_ascii=False) for call in llm.calls)
        roles = [m.role for m in result.messages]
        assert roles == ["user", "assistant", "user", "assistant", "tool", "assistant"]
        assert agent_session.stage == "produce"
        assert result.tool_events[0]["tool"] == "write_copy"
    finally:
        db.close()
