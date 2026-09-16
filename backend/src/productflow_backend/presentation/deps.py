from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from productflow_backend.application.user_accounts import SessionInvalidError, authenticate_session
from productflow_backend.config import get_runtime_settings, get_settings
from productflow_backend.infrastructure.db.models import UserAccount
from productflow_backend.infrastructure.db.session import get_db_session

# 服务端用户会话 cookie（批次 B，与 admin 会话 cookie 相互独立）
USER_SESSION_COOKIE = "pf_user_session"


def get_session(session: Session = Depends(get_db_session)) -> Session:
    return session


def require_admin(request: Request) -> None:
    if not get_runtime_settings().admin_access_required:
        return
    if not request.session.get("is_authenticated"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> UserAccount | None:
    """从服务端会话 cookie 解析当前用户；无会话/会话失效返回 None（不抛错）。"""
    token = request.cookies.get(USER_SESSION_COOKIE)
    if not token:
        return None
    try:
        user, _user_session = authenticate_session(session, token=token)
    except SessionInvalidError:
        return None
    return user


def require_business_user(user: UserAccount | None = Depends(get_current_user)) -> UserAccount | None:
    """业务端点的用户上下文依赖。

    隔离开关关闭：返回 None，端点保持既有行为（现状兼容，整批回退 = 关开关）。
    隔离开关开启：必须有有效用户会话，否则 401；返回当前用户供端点按 owner 过滤。
    """
    if not get_settings().data_isolation_enabled:
        return None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def require_deletion_enabled() -> None:
    if not get_runtime_settings().deletion_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="删除功能已关闭，请联系管理员")
