"""审计批次 A4：统一安全错误结构——密钥抹除、错误分类与路由错误响应。

覆盖：
- infrastructure/safe_errors.py：sanitize_error_text / classify_error 分支；
- tools.execute_tool：原始异常（含密钥）不进工具结果，error 结构化字段替代 detail；
- routes/agent.py：503 响应头（X-Error-Code/X-Retryable/X-Request-Id）与 SSE error 帧的 code/retryable/request_id；
- llm.py 引用迁移后 _summarize_error 行为不回退。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from helpers import _login

from productflow_backend.domain.errors import BusinessValidationError, NotFoundError

# --------------------------------------------------------------------------
# 1. sanitize_error_text：密钥抹除 + 截断
# --------------------------------------------------------------------------


def test_sanitize_error_text_redacts_key_fragments() -> None:
    from productflow_backend.infrastructure.safe_errors import sanitize_error_text

    raw = "provider exploded sk-abc123456789 with api_key=xyz98765 and authorization: Bearer tok123456789"
    cleaned = sanitize_error_text(raw)

    assert "sk-abc" not in cleaned
    assert "xyz98765" not in cleaned
    assert "tok123456789" not in cleaned
    assert "[已抹除]" in cleaned


def test_sanitize_error_text_truncates_to_limit() -> None:
    from productflow_backend.infrastructure.safe_errors import sanitize_error_text

    assert len(sanitize_error_text("x" * 500)) == 200
    assert len(sanitize_error_text("x" * 500, limit=32)) == 32


# --------------------------------------------------------------------------
# 2. classify_error：分支覆盖（timeout / not_found / provider_unavailable / invalid_input / internal）
# --------------------------------------------------------------------------


def _status_exception(status: int, message: str = "provider error") -> Exception:
    """构造带 status_code 属性的供应商风格异常（openai APIStatusError 同款形状）。"""

    class _ProviderError(Exception):
        pass

    exc = _ProviderError(message)
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


def test_classify_error_timeout_branches() -> None:
    from openai import APITimeoutError

    from productflow_backend.infrastructure.safe_errors import classify_error

    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")

    assert classify_error(APITimeoutError(request=request)) == {"code": "timeout", "retryable": True}
    assert classify_error(TimeoutError("upstream read timed out")) == {"code": "timeout", "retryable": True}
    assert classify_error(httpx.ConnectTimeout("timed out")) == {"code": "timeout", "retryable": True}


def test_classify_error_not_found_branches() -> None:
    from openai import NotFoundError as OpenAINotFoundError

    from productflow_backend.infrastructure.safe_errors import classify_error

    request = httpx.Request("GET", "https://provider.test/v1/models")
    openai_404 = OpenAINotFoundError("missing model", response=httpx.Response(404, request=request), body=None)

    assert classify_error(openai_404) == {"code": "not_found", "retryable": False}
    assert classify_error(NotFoundError("会话不存在")) == {"code": "not_found", "retryable": False}
    assert classify_error(_status_exception(404, "gone")) == {"code": "not_found", "retryable": False}


def test_classify_error_provider_unavailable_branches() -> None:
    from openai import APIConnectionError

    from productflow_backend.infrastructure.safe_errors import classify_error

    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")

    assert classify_error(APIConnectionError(request=request)) == {
        "code": "provider_unavailable",
        "retryable": True,
    }
    assert classify_error(ConnectionRefusedError("connection refused")) == {
        "code": "provider_unavailable",
        "retryable": True,
    }
    for status in (502, 503, 504):
        assert classify_error(_status_exception(status)) == {"code": "provider_unavailable", "retryable": True}


def test_classify_error_empty_payload_is_retryable_provider_unavailable() -> None:
    """空载荷（中转站回 200 无 choices）属于可重试的瞬时供应商故障。"""
    from productflow_backend.application.designer_agent.llm import EmptyLLMResponseError
    from productflow_backend.infrastructure.safe_errors import classify_error

    assert classify_error(EmptyLLMResponseError("供应商返回空响应")) == {
        "code": "provider_unavailable",
        "retryable": True,
    }


def test_classify_error_invalid_input_and_internal_branches() -> None:
    from productflow_backend.infrastructure.safe_errors import classify_error

    assert classify_error(ValueError("数量必须在 1~4")) == {"code": "invalid_input", "retryable": False}
    business = BusinessValidationError("当前状态不允许出图")
    assert classify_error(business) == {"code": "invalid_input", "retryable": False}
    assert classify_error(_status_exception(400, "bad request")) == {"code": "invalid_input", "retryable": False}

    leaked = RuntimeError("provider traceback path=/tmp/x sk-abc123456789")
    assert classify_error(leaked) == {"code": "internal", "retryable": False}


def test_classify_error_follows_cause_chain_for_wrapped_errors() -> None:
    """AgentLLMError 式包装异常应按根因分类，而不是一律 internal。"""
    from productflow_backend.infrastructure.safe_errors import classify_error

    try:
        try:
            raise TimeoutError("upstream read timed out")
        except TimeoutError as inner:
            raise RuntimeError("设计师模型调用失败: ...") from inner
    except RuntimeError as wrapped:
        exc = wrapped

    assert classify_error(exc) == {"code": "timeout", "retryable": True}


# --------------------------------------------------------------------------
# 3. llm.py 正则迁移回归：_summarize_error 行为不变
# --------------------------------------------------------------------------


def test_llm_summarize_error_still_redacts_after_pattern_migration() -> None:
    from productflow_backend.application.designer_agent.llm import _summarize_error

    summary = _summarize_error(RuntimeError("gateway sk-abc123456789 exploded"))
    assert "sk-abc" not in summary
    assert summary.startswith("RuntimeError:")
    assert "[已抹除]" in summary


# --------------------------------------------------------------------------
# 4. tools.execute_tool：原始异常不进工具结果，error 结构化字段
# --------------------------------------------------------------------------


class _NoopLLM:
    provider_name = "scripted"
    model = "scripted-model"

    def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], intent: str = "") -> Any:
        raise AssertionError("execute_tool 错误路径不应调用 LLM")


def test_execute_tool_error_hides_raw_exception_and_adds_error_structure(
    db_session, monkeypatch
) -> None:
    from productflow_backend.application.designer_agent import tools as agent_tools
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.designer_agent.tools import execute_tool

    def _explode(ctx) -> dict[str, Any]:
        raise RuntimeError("provider traceback path=/tmp/trace sk-abc123456789 api_key=secret98765")

    monkeypatch.setitem(agent_tools._TOOL_HANDLERS, "write_copy", _explode)
    agent_session = create_agent_session(db_session, title="密钥泄露防护")

    result = execute_tool(db_session, agent_session, name="write_copy", arguments={}, llm=_NoopLLM())
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "error"
    # 原始异常全文（含密钥）一个字都不许出现在工具结果里（会进 LLM 上下文并落库）
    assert "sk-abc" not in serialized
    assert "api_key" not in serialized
    assert "/tmp/trace" not in serialized
    assert "detail" not in result, "原始异常摘要字段必须删除"
    # 人话 message 保持不变，新增结构化错误分类
    assert result["message"] == "生成请求没有成功，我会换个方式再试一次。"
    assert result["error"] == {"code": "internal", "retryable": False}


def test_execute_tool_timeout_error_is_classified_retryable(db_session, monkeypatch) -> None:
    from productflow_backend.application.designer_agent import tools as agent_tools
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.designer_agent.tools import execute_tool

    def _timeout(ctx) -> dict[str, Any]:
        raise TimeoutError("upstream read timed out")

    monkeypatch.setitem(agent_tools._TOOL_HANDLERS, "generate_image", _timeout)
    agent_session = create_agent_session(db_session, title="超时分类")

    result = execute_tool(db_session, agent_session, name="generate_image", arguments={}, llm=_NoopLLM())

    assert result["status"] == "error"
    assert result["error"] == {"code": "timeout", "retryable": True}


# --------------------------------------------------------------------------
# 5. routes/agent.py：503 错误头 + SSE error 帧结构化字段
# --------------------------------------------------------------------------


def _logged_in_client() -> TestClient:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    _login(client)
    return client


def test_agent_message_503_exposes_error_headers(configured_env: Path) -> None:
    """mock 供应商（AgentLLMError）→ 503 人话 detail + X-Error-Code/X-Retryable/X-Request-Id 头。"""
    client = _logged_in_client()
    session_id = client.post("/api/agent/sessions", json={"title": "503 错误头"}).json()["id"]

    response = client.post(f"/api/agent/sessions/{session_id}/messages", json={"content": "来一张开业海报"})

    assert response.status_code == 503
    assert response.json()["detail"] == "设计师模型暂时没有响应，请稍等片刻再试一次。"
    assert response.headers["X-Error-Code"] == "provider_unavailable"
    assert response.headers["X-Retryable"] == "true"
    assert response.headers["X-Request-Id"] not in ("", "-")


def _parse_sse_frames(body: str) -> dict[str, list[dict]]:
    frames: dict[str, list[dict]] = {}
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        event = next(line[len("event: ") :] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[len("data: ") :] for line in lines if line.startswith("data: ")))
        frames.setdefault(event, []).append(data)
    return frames


def test_agent_stream_error_frame_carries_code_retryable_and_request_id(configured_env: Path) -> None:
    """SSE error 帧：message 人话不变，新增 code/retryable/request_id。"""
    client = _logged_in_client()
    session_id = client.post("/api/agent/sessions", json={}).json()["id"]

    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/messages/stream",
        json={"content": "你好"},
    ) as response:
        body = "".join(response.iter_text())

    frames = _parse_sse_frames(body)
    error_data = frames["error"][0]
    assert error_data["message"] == "设计师模型暂时没有响应，请稍等片刻再试一次。"
    assert error_data["code"] == "provider_unavailable"
    assert error_data["retryable"] is True
    assert error_data["request_id"] not in ("", "-")
    assert any(event == "done" for event in frames)


def test_agent_stream_error_frame_classifies_domain_errors(configured_env: Path) -> None:
    """业务类错误走 classify_error：未知会话 → not_found，空消息 → invalid_input。"""
    client = _logged_in_client()

    with client.stream(
        "POST",
        "/api/agent/sessions/no-such-session/messages/stream",
        json={"content": "在吗"},
    ) as response:
        missing = _parse_sse_frames("".join(response.iter_text()))

    assert missing["error"][0]["code"] == "not_found"
    assert missing["error"][0]["retryable"] is False
    assert missing["error"][0]["message"]

    session_id = client.post("/api/agent/sessions", json={}).json()["id"]
    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/messages/stream",
        json={"content": "   "},
    ) as response:
        empty = _parse_sse_frames("".join(response.iter_text()))

    assert empty["error"][0]["code"] == "invalid_input"
    assert empty["error"][0]["retryable"] is False
