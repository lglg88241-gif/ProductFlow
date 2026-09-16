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


def require_admin(request: Request, session: Session = Depends(get_session)) -> None:
    """业务与管理接口的门禁（兼容旧共享口令）。

    复审 P1-三：用户账号登录写入的是 pf_user_session，不设置旧门禁使用的
    request.session.is_authenticated——于是"生产要求门禁开启 + 用户已登录"的组合下，
    普通用户连**自己的**商品工作流都读不到（401）。

    这里保持旧门禁语义不变（关闭即放行、开启即要求身份），但把"有效身份"扩展到
    两种：旧的共享口令会话，或有效的用户账号会话。业务接口的**越权**由
    require_business_user + 归属守卫负责，门禁只回答"是否已识别身份"。
    """
    if not get_runtime_settings().admin_access_required:
        return
    if request.session.get("is_authenticated"):
        return
    # 有效用户账号会话同样视为已识别身份（不让共享口令成为普通用户的额外必需凭据）
    if get_current_user(request, session) is not None:
        return
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


def require_account_admin(
    request: Request,
    user: UserAccount | None = Depends(get_current_user),
) -> UserAccount | None:
    """账号管理员：要求**真实身份**，与旧门禁开关无关。

    复审 P1（已复现）：邀请管理、诊断等管理接口此前只挂 require_admin，而
    require_admin 在 ADMIN_ACCESS_REQUIRED=false 时**无条件放行**——匿名客户端
    带一个公开可构造的 CSRF 头即可创建邀请并拿到明文 token 自助开户。
    CSRF 头只是防跨站，不能代替身份认证。

    身份判定（按序）：
      1. 有效用户账号会话且 role=admin → 通过（新模型，推荐路径）；
      2. 已认证的旧共享口令会话 → 通过。这是**限定范围的兼容路径**：仅在运维方
         显式用管理员口令登录后成立（旧门禁开启时需要口令、关闭时需要有人先登过），
         用于初始引导与过渡期；匿名客户端两种都不满足，一律 401。
    非 admin 的用户账号 → 403（已识别身份但无管理权）。
    """
    if user is not None:
        if user.role == "admin":
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    if request.session.get("is_authenticated"):
        return None
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
