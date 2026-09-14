from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from openai import OpenAI

from productflow_backend.infrastructure.provider_config import resolve_agent_provider_config

logger = logging.getLogger(__name__)

# 降级日志只输出错误摘要：限长并抹除疑似密钥片段，严防 API key 进入日志
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_.-]{6,}|api[-_]?key\s*[=:]\s*\S+|authorization[:\s]*bearer\s+\S+)"
)
_ERROR_SUMMARY_MAX_CHARS = 200


class AgentLLMError(RuntimeError):
    """Agent 底层 LLM 调用失败（网络/供应商错误）。"""


# 中转站常见的瞬时故障（网关 5xx / 连接类）重试一次；超时不重试，避免等待翻倍
AGENT_LLM_TRANSIENT_RETRIES = 1
AGENT_LLM_RETRY_DELAY_SECONDS = 2.0
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 520, 521, 522, 523, 524}


def _is_transient_llm_error(exc: Exception) -> bool:
    """仅网络类/网关类瞬时故障可重试；超时（等待成本高）与 4xx 输入错误不重试。"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS_CODES:
        return True
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError"}:
        return name != "APITimeoutError"
    message = str(exc)
    return any(code in message for code in (" 502", " 503 ", " 500 ", " 504"))


def _summarize_error(exc: Exception, *, limit: int = _ERROR_SUMMARY_MAX_CHARS) -> str:
    """生成可安全落日志的错误摘要：限长并抹除疑似密钥片段。"""
    text = _SENSITIVE_KEY_PATTERN.sub("[已抹除]", f"{type(exc).__name__}: {exc}")
    return text[:limit]


def _parse_usage(usage: Any) -> dict[str, int] | None:
    """从 OpenAI 兼容响应解析 token 用量；响应缺失 usage 或字段全空时返回 None。"""
    if usage is None:
        return None

    def _get(key: str) -> int | None:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        return int(value) if isinstance(value, int) else None

    prompt_tokens = _get("prompt_tokens")
    completion_tokens = _get("completion_tokens")
    total_tokens = _get("total_tokens")
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None
    if total_tokens is None:
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    return {
        "prompt_tokens": prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "total_tokens": total_tokens,
    }


def _mark_served_by(client: AgentLLMClient, response: AgentLLMResponse) -> AgentLLMResponse:
    """在响应上标记实际服务方（provider/模型），供降级链观测。"""
    return replace(response, served_by=f"{client.provider_name}/{response.model or client.model}")


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
    # token 用量（prompt_tokens/completion_tokens/total_tokens）；供应商未返回时为 None
    usage: dict[str, int] | None = None
    # 实际服务方（provider/model）；降级链由 FallbackAgentLLMClient 标注
    served_by: str | None = None


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
        started = time.perf_counter()
        response = None
        last_exc: Exception | None = None
        for attempt in range(AGENT_LLM_TRANSIENT_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools or None,  # type: ignore[arg-type]
                    temperature=0.4,
                )
                break
            except Exception as exc:  # noqa: BLE001 - 供应商异常统一转译为 AgentLLMError
                last_exc = exc
                if not _is_transient_llm_error(exc) or attempt >= AGENT_LLM_TRANSIENT_RETRIES:
                    raise AgentLLMError(f"设计师模型调用失败: {exc}") from exc
                logger.warning(
                    "设计师模型瞬时故障，重试: attempt=%s error=%s", attempt + 1, type(exc).__name__
                )
                time.sleep(AGENT_LLM_RETRY_DELAY_SECONDS)
        if response is None:
            raise AgentLLMError(f"设计师模型调用失败: {last_exc}") from last_exc
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
        logger.info(
            "设计师模型调用完成: provider=%s model=%s duration_ms=%.0f",
            self.provider_name,
            response.model or self.model,
            (time.perf_counter() - started) * 1000,
        )
        return AgentLLMResponse(
            content=choice.content,
            tool_calls=tool_calls,
            model=response.model or self.model,
            usage=_parse_usage(getattr(response, "usage", None)),
        )


class FallbackAgentLLMClient:
    """主供应商失败时自动降级到备用供应商（如 Grok → Gemini Flash）。"""

    def __init__(self, primary: AgentLLMClient, fallback: AgentLLMClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.provider_name = f"{primary.provider_name}->{fallback.provider_name}"
        self.model = primary.model

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: str = "",
    ) -> AgentLLMResponse:
        try:
            response = self.primary.chat(messages=messages, tools=tools, intent=intent)
        except AgentLLMError as exc:
            # 降级切换必须可观测：记录主用失败原因摘要（已抹除疑似密钥），绝不静默
            logger.warning(
                "设计师模型主用供应商失败，已切换降级供应商: primary=%s reason=%s",
                self.primary.provider_name,
                _summarize_error(exc),
            )
            response = self.fallback.chat(messages=messages, tools=tools, intent=intent)
            return _mark_served_by(self.fallback, response)
        return _mark_served_by(self.primary, response)


def build_agent_llm_client() -> AgentLLMClient:
    """按 agent 供应商绑定解析连接配置：主供应商 + 可选降级；mock 绑定明确报错。"""
    config = resolve_agent_provider_config()
    if config.provider_kind == "mock":
        raise AgentLLMError(
            "设计师 Agent 供应商未配置：可在 .env 填写 AGENT_*（地址/key/模型），或在系统设置中绑定（当前为 mock）"
        )
    if not config.api_key:
        raise AgentLLMError("设计师 Agent 的供应商缺少 API Key，请在系统设置中补全")
    primary = OpenAICompatAgentClient(
        provider_name=config.provider_kind,
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )
    if not config.fallback_provider_profile_id:
        return primary
    if not config.fallback_api_key or not config.fallback_model:
        raise AgentLLMError("降级供应商配置不完整（缺 API Key 或 fallback_model），请在系统设置中补全")
    fallback = OpenAICompatAgentClient(
        provider_name=f"{config.provider_kind}:fallback",
        api_key=config.fallback_api_key,
        base_url=config.fallback_base_url,
        model=config.fallback_model,
    )
    return FallbackAgentLLMClient(primary=primary, fallback=fallback)
