from __future__ import annotations

import math
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response, status

from productflow_backend.config import get_runtime_settings, get_settings
from productflow_backend.presentation.schemas.auth import (
    SessionCreateRequest,
    SessionResponse,
    SessionStateResponse,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 登录失败限速（内存级，按客户端 IP）：15 分钟窗口内第 6 次失败起返回 429
_LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
_LOGIN_FAILURE_LIMIT = 5
_login_failure_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _prune_login_failures(now: float) -> None:
    """清理窗口外的失败记录，避免长期运行时内存无限增长。"""
    expired_before = now - _LOGIN_FAILURE_WINDOW_SECONDS
    for ip in list(_login_failures):
        recent = [ts for ts in _login_failures[ip] if ts > expired_before]
        if recent:
            _login_failures[ip] = recent
        else:
            _login_failures.pop(ip, None)


def _reject_if_rate_limited(ip: str) -> None:
    """窗口内失败次数达到上限时拒绝（带 Retry-After），未达上限则放行。"""
    now = time.monotonic()
    with _login_failure_lock:
        _prune_login_failures(now)
        recent = _login_failures.get(ip, [])
        if len(recent) < _LOGIN_FAILURE_LIMIT:
            return
        retry_after = max(1, math.ceil(recent[0] + _LOGIN_FAILURE_WINDOW_SECONDS - now))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="登录失败次数过多，请 15 分钟后再试",
        headers={"Retry-After": str(retry_after)},
    )


def _record_login_failure(ip: str) -> None:
    with _login_failure_lock:
        _login_failures.setdefault(ip, []).append(time.monotonic())


def _clear_login_failures(ip: str) -> None:
    with _login_failure_lock:
        _login_failures.pop(ip, None)


@router.post("/session", response_model=SessionResponse)
def create_session(payload: SessionCreateRequest, request: Request) -> SessionResponse:
    if not get_runtime_settings().admin_access_required:
        request.session["is_authenticated"] = True
        return SessionResponse()
    ip = _client_ip(request)
    _reject_if_rate_limited(ip)
    settings = get_settings()
    if payload.admin_key != settings.admin_access_key:
        _record_login_failure(ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员密钥不正确")
    _clear_login_failures(ip)
    request.session.clear()
    request.session["is_authenticated"] = True
    return SessionResponse()


@router.get("/session", response_model=SessionStateResponse)
def get_session_state(request: Request) -> SessionStateResponse:
    access_required = get_runtime_settings().admin_access_required
    return SessionStateResponse(
        authenticated=not access_required or bool(request.session.get("is_authenticated")),
        access_required=access_required,
    )


@router.delete("/session", response_model=SessionResponse)
def destroy_session(request: Request, response: Response) -> SessionResponse:
    request.session.clear()
    response.delete_cookie("session")
    return SessionResponse()
