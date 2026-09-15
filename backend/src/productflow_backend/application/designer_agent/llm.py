from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from openai import OpenAI

from productflow_backend.infrastructure.openai_client_cache import KeyedClientCache
from productflow_backend.infrastructure.provider_config import resolve_agent_provider_config

logger = logging.getLogger(__name__)

# Agent 轮次超时预算：客户端构造参数与调用语义保持不变，仅实例改为按连接参数复用
_AGENT_LLM_TIMEOUT_SECONDS = 120.0
# 按 (api_key, base_url, timeout) 复用 OpenAI 客户端：避免每轮重建 httpx 连接池
_OPENAI_CLIENTS = KeyedClientCache()

# 降级日志只输出错误摘要：限长并抹除疑似密钥片段，严防 API key 进入日志
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_.-]{6,}|api[-_]?key\s*[=:]\s*\S+|authorization[:\s]*bearer\s+\S+)"
)
_ERROR_SUMMARY_MAX_CHARS = 200


class AgentLLMError(RuntimeError):
    """Agent 底层 LLM 调用失败（网络/供应商错误）。"""


# 中转站常见的瞬时故障（网关 5xx / 连接类）重试一次；超时不重试，避免等待翻倍
# 瞬时故障重试的默认策略；实际取值由 build_agent_llm_client 从 Settings 注入，
# 客户端本身不读全局配置（否则单独构造客户端就得备齐 DATABASE_URL 等全部环境）。
DEFAULT_TRANSIENT_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 1.0
# 整轮 LLM 调用的总时长预算：必须低于网关（nginx proxy_read_timeout 300s），
# 否则"重试次数 × 单次超时"会让请求在网关侧被切断成 504。
DEFAULT_TOTAL_BUDGET_SECONDS = 240.0


@dataclass(frozen=True)
class TransientRetryPolicy:
    """指数退避 + 抖动。

    中转站过载时表现为 502 突发（实测同一时刻单发探测 200、连续调用 502），
    固定短延迟重试会正好落在坏窗口里；指数退避把重试摊到更长的时间轴上，
    抖动避免多个并发 turn 同时重试形成二次冲击。
    """

    max_retries: int = DEFAULT_TRANSIENT_RETRIES
    base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    total_budget_seconds: float = DEFAULT_TOTAL_BUDGET_SECONDS

    def delay_for(self, attempt: int) -> float:
        return self.base_delay_seconds * (2**attempt) + random.uniform(0, self.base_delay_seconds)

    def budget_exhausted(self, elapsed_seconds: float) -> bool:
        """总预算是否已耗尽——耗尽后不再重试，避免把请求拖到网关超时。"""
        return elapsed_seconds >= self.total_budget_seconds


_TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 520, 521, 522, 523, 524}


class EmptyLLMResponseError(Exception):
    """供应商返回 HTTP 200 但载荷里没有 choices（中转站空响应）。

    与生图侧 `_parse_with_empty_output_retry` 同类问题：中转站偶发回空载荷，
    若直接取 choices[0] 会抛 IndexError，逃过 AgentLLMError 处理变成 500。
    这里显式建模为"可重试的瞬时故障"。
    """


def _extract_choice(response: Any) -> Any:
    """取出首个 choice；空载荷抛 EmptyLLMResponseError 以便走同一重试路径。"""
    choices = getattr(response, "choices", None)
    if not choices:
        raise EmptyLLMResponseError("供应商返回空响应（无 choices）")
    choice = getattr(choices[0], "message", None)
    if choice is None:
        raise EmptyLLMResponseError("供应商返回的 choice 缺少 message")
    return choice


def _is_transient_llm_error(exc: Exception) -> bool:
    """仅网络类/网关类瞬时故障可重试；超时（等待成本高）与 4xx 输入错误不重试。"""
    if isinstance(exc, EmptyLLMResponseError):
        return True
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

    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str,
        base_url: str | None,
        model: str,
        retry_policy: TransientRetryPolicy | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.retry_policy = retry_policy or TransientRetryPolicy()
        # 保留地址便于诊断（降级切换/排障时需要知道实际打的是哪个中转站）
        self.base_url = base_url
        cache_key = (api_key, base_url or None, _AGENT_LLM_TIMEOUT_SECONDS)
        self._client = _OPENAI_CLIENTS.get_or_create(
            cache_key,
            lambda: OpenAI(api_key=api_key, base_url=base_url, timeout=_AGENT_LLM_TIMEOUT_SECONDS),
        )

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
        choice = None
        last_exc: Exception | None = None
        max_retries = self.retry_policy.max_retries
        for attempt in range(max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools or None,  # type: ignore[arg-type]
                    temperature=0.4,
                )
                # 空载荷必须在 try 内提取：否则 IndexError 会绕过重试与 AgentLLMError 处理
                choice = _extract_choice(response)
                break
            except Exception as exc:  # noqa: BLE001 - 供应商异常统一转译为 AgentLLMError
                last_exc = exc
                elapsed = time.perf_counter() - started
                if (
                    not _is_transient_llm_error(exc)
                    or attempt >= max_retries
                    or self.retry_policy.budget_exhausted(elapsed)
                ):
                    raise AgentLLMError(f"设计师模型调用失败: {exc}") from exc
                delay = self.retry_policy.delay_for(attempt)
                logger.warning(
                    "设计师模型瞬时故障，重试: attempt=%s/%s error=%s delay_s=%.1f elapsed_s=%.0f",
                    attempt + 1,
                    max_retries,
                    type(exc).__name__,
                    delay,
                    elapsed,
                )
                time.sleep(delay)
        if response is None or choice is None:
            raise AgentLLMError(f"设计师模型调用失败: {last_exc}") from last_exc
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
    from productflow_backend.config import get_settings

    settings = get_settings()
    retry_policy = TransientRetryPolicy(
        max_retries=settings.agent_llm_transient_retries,
        base_delay_seconds=settings.agent_llm_retry_base_delay_seconds,
        total_budget_seconds=settings.agent_llm_total_budget_seconds,
    )
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
        retry_policy=retry_policy,
    )
    # 降级是否可用看"有没有 key + 模型"，不能看 fallback_provider_profile_id：
    # 后者只在界面绑定路径产生，.env 直填（中转场景）永远拿不到它——
    # 历史实现因此在 env-first 下静默丢弃用户已经配好的降级链。
    if not config.fallback_api_key or not config.fallback_model:
        if config.fallback_provider_profile_id:
            raise AgentLLMError("降级供应商配置不完整（缺 API Key 或 fallback_model），请在系统设置中补全")
        return primary
    fallback = OpenAICompatAgentClient(
        provider_name=f"{config.provider_kind}:fallback",
        api_key=config.fallback_api_key,
        # 同站点换模型是最常见形态：未单独给 fallback 地址时沿用主供应商地址，
        # 避免静默落到默认公网地址。
        base_url=config.fallback_base_url or config.base_url,
        model=config.fallback_model,
        retry_policy=retry_policy,
    )
    return FallbackAgentLLMClient(primary=primary, fallback=fallback)
