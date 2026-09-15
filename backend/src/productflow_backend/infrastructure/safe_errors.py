"""统一的安全错误结构（审计批次 A4）：密钥抹除 + 错误分类。

消费方：
- application/designer_agent/tools.py：工具异常 → 人话 message + {"code", "retryable"} 结构化字段，
  原始异常文本绝不进入 LLM 上下文或落库消息；
- presentation/routes/agent.py：503 响应头（X-Error-Code/X-Retryable）与 SSE error 帧的 code/retryable；
- application/designer_agent/llm.py：降级日志摘要的密钥抹除（SENSITIVE_KEY_PATTERN 的原主）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from productflow_backend.domain.errors import BusinessError, NotFoundError

# 疑似密钥片段：sk- 开头 key / api_key=... / Authorization Bearer（自 llm.py 迁移为公开常量，行为不变）
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_.-]{6,}|api[-_]?key\s*[=:]\s*\S+|authorization[:\s]*bearer\s+\S+)"
)
_REDACTED = "[已抹除]"

# 供应商/网关不可用状态码：标准网关 5xx + 中转站 52x + 容量饱和（429）
_PROVIDER_UNAVAILABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# 连接类错误类型名（httpx/openai 的连接异常不继承内建 ConnectionError，需按名匹配）
_CONNECTION_TYPE_NAMES = {"APIConnectionError", "APIConnectError", "ConnectError"}
# 包装异常（如 AgentLLMError）按根因（__cause__）分类时的链上深度上限
_MAX_CAUSE_DEPTH = 3


def sanitize_error_text(text: str, limit: int = 200) -> str:
    """抹除疑似密钥片段并截断：输出仅用于日志/诊断，绝不进入用户可见内容或 LLM 上下文。"""
    return SENSITIVE_KEY_PATTERN.sub(_REDACTED, text)[:limit]


def classify_error(exc: Exception) -> dict[str, Any]:
    """把异常映射为稳定错误结构 {"code", "retryable"}；包装异常按根因（__cause__）分类。

    code 约定：
    - timeout              超时（openai APITimeoutError / 内建 TimeoutError / httpx *Timeout）；retryable
    - provider_unavailable 网关 5xx（502/503/504/52x）、连接失败、空载荷、容量饱和（429）；retryable
    - not_found            404 / NotFoundError
    - invalid_input        ValueError / BusinessError 及其余 4xx
    - internal             其余
    """
    for candidate in _error_chain(exc):
        classified = _classify_single(candidate)
        if classified is not None:
            return classified
    return {"code": "internal", "retryable": False}


def _error_chain(exc: Exception) -> Iterator[Exception]:
    """遍历异常与其显式根因链（有界深度、防环）。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, Exception):
            yield current
        current = current.__cause__


def _classify_single(exc: Exception) -> dict[str, Any] | None:
    """单层异常分类；无明确归类（≈internal）时返回 None，交由根因链继续判定。"""
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return {"code": "timeout", "retryable": True}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 404:
            return {"code": "not_found", "retryable": False}
        if status in _PROVIDER_UNAVAILABLE_STATUS:
            return {"code": "provider_unavailable", "retryable": True}
        if 400 <= status < 500:
            return {"code": "invalid_input", "retryable": False}
    if isinstance(exc, NotFoundError):
        return {"code": "not_found", "retryable": False}
    if isinstance(exc, (ValueError, BusinessError)):
        return {"code": "invalid_input", "retryable": False}
    if _is_provider_unavailable(exc):
        return {"code": "provider_unavailable", "retryable": True}
    return None


def _is_provider_unavailable(exc: Exception) -> bool:
    """连接失败 / 瞬时网关故障 / 空载荷判定（复用 llm.py 的瞬时故障逻辑，避免复制）。"""
    if isinstance(exc, ConnectionError):
        return True
    if type(exc).__name__ in _CONNECTION_TYPE_NAMES:
        return True
    # 函数内延迟导入：llm.py 顶层反向依赖本模块（取正则），顶层互导会成环
    from productflow_backend.application.designer_agent.llm import _is_transient_llm_error

    return _is_transient_llm_error(exc)
