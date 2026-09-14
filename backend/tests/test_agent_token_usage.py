"""Agent token 用量落库与结构化日志：usage 解析、降级切换可观测、工具计时。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from productflow_backend.application.designer_agent.llm import (
    AgentLLMError,
    AgentLLMResponse,
    AgentToolCall,
    FallbackAgentLLMClient,
    OpenAICompatAgentClient,
)


class _UsageScriptedLLM:
    """带 usage 的剧本化假 Agent LLM（视觉调用走固定回复，不消耗剧本）。"""

    provider_name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[AgentLLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, *, messages, tools, intent: str = "") -> AgentLLMResponse:
        self.calls.append({"messages": [dict(item) for item in messages], "tools": tools, "intent": intent})
        content = messages[-1].get("content") if messages else None
        if isinstance(content, list):
            return AgentLLMResponse(content=json.dumps({}), model=self.model)
        if not self.script:
            return AgentLLMResponse(content="好的。", model=self.model)
        return self.script.pop(0)


class _FixedLLM:
    """行为固定的假 LLM：要么抛错要么返回固定响应。"""

    def __init__(self, *, name: str, model: str, error: str | None = None) -> None:
        self.provider_name = name
        self.model = model
        self.error = error
        self.calls = 0

    def chat(self, *, messages, tools, intent: str = "") -> AgentLLMResponse:
        self.calls += 1
        if self.error:
            raise AgentLLMError(self.error)
        return AgentLLMResponse(content=f"from-{self.provider_name}", model=self.model)


def _openai_compat_client_with(completions) -> OpenAICompatAgentClient:
    client = OpenAICompatAgentClient(provider_name="t", api_key="k", base_url="http://x", model="m")
    client._client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return client


def _completion_response(*, usage) -> object:
    message = type("M", (), {"content": "ok", "tool_calls": None})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()], "model": "m", "usage": usage})()


class _OnceCompletions:
    """单次成功返回给定响应的假 completions。"""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self.response


def test_openai_compat_client_parses_usage_from_response() -> None:
    usage = type("U", (), {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})()
    completions = _OnceCompletions(_completion_response(usage=usage))
    client = _openai_compat_client_with(completions)

    response = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert response.content == "ok"
    assert response.usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def test_openai_compat_client_usage_missing_returns_none() -> None:
    # 响应不带 usage 字段（部分中转/旧网关），解析应安全返回 None
    completions = _OnceCompletions(_completion_response(usage=None))
    client = _openai_compat_client_with(completions)

    response = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert response.usage is None


def test_openai_compat_client_logs_model_and_duration(caplog: pytest.LogCaptureFixture) -> None:
    usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})()
    completions = _OnceCompletions(_completion_response(usage=usage))
    client = _openai_compat_client_with(completions)

    with caplog.at_level(logging.INFO, logger="productflow_backend.application.designer_agent.llm"):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert any(
        "设计师模型调用完成" in record.message and "provider=t" in record.message and "model=m" in record.message
        and "duration_ms=" in record.message
        for record in caplog.records
    )


def test_fallback_client_marks_served_by_and_warns_without_key_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = _FixedLLM(name="grok", model="grok-4.6", error="502 bad gateway api_key=sk-live-abcdef1234567890")
    fallback = _FixedLLM(name="gemini", model="gemini-3.8-flash")
    chain = FallbackAgentLLMClient(primary=primary, fallback=fallback)

    with caplog.at_level(logging.WARNING, logger="productflow_backend.application.designer_agent.llm"):
        response = chain.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    # 降级必须可观测：warning 含主用方与失败摘要
    warnings = [record for record in caplog.records if "已切换降级供应商" in record.message]
    assert warnings, "降级切换应输出 warning 日志"
    assert "grok" in warnings[0].getMessage()
    assert "502" in warnings[0].getMessage()
    # 严禁密钥进入日志
    assert "sk-live-abcdef1234567890" not in caplog.text
    assert "super-secret" not in caplog.text
    # 结果上标记实际服务方
    assert response.served_by == "gemini/gemini-3.8-flash"
    assert response.content == "from-gemini"


def test_fallback_client_marks_primary_when_healthy(caplog: pytest.LogCaptureFixture) -> None:
    primary = _FixedLLM(name="grok", model="grok-4.6")
    fallback = _FixedLLM(name="gemini", model="gemini-3.8-flash")
    chain = FallbackAgentLLMClient(primary=primary, fallback=fallback)

    with caplog.at_level(logging.WARNING, logger="productflow_backend.application.designer_agent.llm"):
        response = chain.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert response.served_by == "grok/grok-4.6"
    assert not [record for record in caplog.records if "已切换降级供应商" in record.message]


def test_agent_turn_persists_usage_on_assistant_messages(
    configured_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    llm = _UsageScriptedLLM(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(call_id="call-tok-1", name="generate_image",
                                  arguments={"prompt": "开业海报", "size": "1080x1440"})
                ],
                model="scripted-model",
                usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            ),
            AgentLLMResponse(
                content="图片已经生成好了！要换配色或尺寸吗？",
                model="scripted-model",
                usage={"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240},
            ),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="token 落库")
        with caplog.at_level(logging.INFO, logger="productflow_backend.application.designer_agent.loop"):
            result = run_agent_turn(
                db, agent_session_id=agent_session.id, user_content="出一张开业海报", llm=llm
            )

        roles = [message.role for message in result.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        tool_call_message, tool_message, final_message = result.messages[1], result.messages[2], result.messages[3]
        # 两次 LLM 响应的 usage 分别落到两条 assistant 消息
        assert (
            tool_call_message.prompt_tokens, tool_call_message.completion_tokens, tool_call_message.total_tokens
        ) == (120, 30, 150)
        assert (
            final_message.prompt_tokens, final_message.completion_tokens, final_message.total_tokens
        ) == (200, 40, 240)
        # user 与 tool 消息不落 token
        assert result.messages[0].total_tokens is None
        assert tool_message.total_tokens is None

        # 工具执行计时日志（logger.info：工具名 + 耗时）
        assert any(
            "Agent 工具执行完成" in record.message and "generate_image" in record.message
            for record in caplog.records
        )
    finally:
        db.close()


def test_agent_turn_events_persists_usage_on_assistant_messages(configured_env: Path) -> None:
    from sqlalchemy import select

    from productflow_backend.application.designer_agent.loop import (
        create_agent_session,
        run_agent_turn_events,
    )
    from productflow_backend.infrastructure.db.models import AgentMessage
    from productflow_backend.infrastructure.db.session import get_session_factory

    llm = _UsageScriptedLLM(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(call_id="call-tok-2", name="generate_image",
                                  arguments={"prompt": "开业海报", "size": "1080x1440"})
                ],
                model="scripted-model",
                usage={"prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21},
            ),
            AgentLLMResponse(
                content="图好了。",
                model="scripted-model",
                usage={"prompt_tokens": 25, "completion_tokens": 8, "total_tokens": 33},
            ),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="token 落库(流式)")
        events = list(
            run_agent_turn_events(db, agent_session_id=agent_session.id, user_content="出一张海报", llm=llm)
        )
        assert any(event["event"] == "done" for event in events)

        messages = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == agent_session.id)
                .order_by(AgentMessage.created_at, AgentMessage.id)
            )
        )
        assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
        tool_call_message, final_message = messages[1], messages[3]
        assert (tool_call_message.prompt_tokens, tool_call_message.total_tokens) == (15, 21)
        assert (final_message.prompt_tokens, final_message.total_tokens) == (25, 33)
        assert messages[0].total_tokens is None and messages[2].total_tokens is None
    finally:
        db.close()
