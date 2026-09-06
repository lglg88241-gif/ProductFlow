from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import OpenAI

from productflow_backend.infrastructure.provider_config import resolve_text_provider_config


class AgentLLMError(RuntimeError):
    """Agent 底层 LLM 调用失败（网络/供应商错误）。"""


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentLLMResponse:
    content: str | None
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    model: str = ""


class AgentLLMClient(Protocol):
    """Agent 编排循环依赖的最小 LLM 接口，测试注入脚本化假实现。"""

    provider_name: str
    model: str

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: str = "",
    ) -> AgentLLMResponse: ...


class OpenAICompatAgentClient:
    """OpenAI 兼容 chat-completions 客户端，连接配置复用 text 供应商档案。"""

    def __init__(self, *, provider_name: str, api_key: str, base_url: str | None, model: str) -> None:
        self.provider_name = provider_name
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: str = "",
    ) -> AgentLLMResponse:
        _ = intent  # 工具内部调用的意图标记仅用于测试分流，生产实现忽略
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools or None,  # type: ignore[arg-type]
                temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001 - 供应商异常统一转译为 AgentLLMError
            raise AgentLLMError(f"设计师模型调用失败: {exc}") from exc
        choice = response.choices[0].message
        tool_calls: list[AgentToolCall] = []
        for call in choice.tool_calls or []:
            function = call.function
            try:
                arguments = json.loads(function.arguments or "{}")
            except (TypeError, ValueError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(AgentToolCall(call_id=call.id or "", name=function.name or "", arguments=arguments))
        return AgentLLMResponse(content=choice.content, tool_calls=tool_calls, model=response.model or self.model)


def build_agent_llm_client() -> AgentLLMClient:
    """按 text 供应商绑定解析连接配置；mock 绑定不支持 Agent，明确报错。"""
    config = resolve_text_provider_config()
    if config.provider_kind == "mock":
        raise AgentLLMError("设计师 Agent 需要 OpenAI 兼容文本供应商，请在系统设置中配置（当前为 mock）")
    if not config.api_key:
        raise AgentLLMError("设计师 Agent 的文本供应商缺少 API Key，请在系统设置中补全")
    return OpenAICompatAgentClient(
        provider_name=config.provider_kind,
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.copy_model,
    )
