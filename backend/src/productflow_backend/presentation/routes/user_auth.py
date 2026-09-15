"""独立账号身份端点（批次 B 第一批，纯新增 router）。

- POST/GET/DELETE /api/auth/invites、POST /api/auth/invite/redeem
- POST /api/auth/user/login、POST /api/auth/user/logout、GET /api/auth/user/me

与现有 admin-key 会话（routes/auth.py → /api/auth/session）完全独立：
- 邀请管理沿用现有 ``require_admin`` 门禁（旧管理员口令）；
- 用户登录/会话走独立的服务端会话 cookie ``pf_user_session``；
- CSRF 最小防线：所有状态变更端点要求自定义头 ``X-Requested-With: productflow``，
  且若请求带 Origin 头，其 host 必须与 Host 一致，否则 403；
- 登录/兑换入口各自独立接入 EntryRateLimiter（实例见 rate_limit.py）。
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from productflow_backend.application.user_accounts import (
    SESSION_ABSOLUTE_LIFETIME,
    InvalidCredentialsError,
    InvalidUserDataError,
    InviteInvalidError,
    SessionInvalidError,
    UsernameTakenError,
    authenticate_session,
    create_invite,
    list_invites,
    login_with_password,
    redeem_invite,
    revoke_invite,
    revoke_user_session,
)
from productflow_backend.config import get_settings
from productflow_backend.infrastructure.db.models import UserInvite
from productflow_backend.presentation.deps import get_session, require_admin
from productflow_backend.presentation.rate_limit import (
    client_ip,
    user_login_entry_rate_limiter,
    user_redeem_entry_rate_limiter,
)
from productflow_backend.presentation.schemas.user_auth import (
    InviteCreatedResponse,
    InviteCreateRequest,
    InviteListItem,
    InviteListResponse,
    InviteRedeemRequest,
    InviteRevokeResponse,
    OkResponse,
    UserLoginRequest,
    UserResponse,
)

router = APIRouter(prefix="/api/auth", tags=["user-accounts"])

USER_SESSION_COOKIE = "pf_user_session"

# CSRF 最小防线（审计要求的可独立撤除项）：状态变更端点必须带的自定义头
CSRF_HEADER_NAME = "x-requested-with"
CSRF_HEADER_VALUE = "productflow"
CSRF_FAILURE_DETAIL = "请求缺少必要的来源校验信息"


def _enforce_csrf(request: Request) -> None:
    """缺自定义头 → 403；带 Origin 但与 Host 不同源 → 403。"""
    header_value = request.headers.get(CSRF_HEADER_NAME, "")
    if header_value != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_FAILURE_DETAIL)
    origin = request.headers.get("origin")
    if origin is not None:
        host = (request.headers.get("host") or "").strip().lower()
        if not host or _origin_netloc(origin) != host:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_FAILURE_DETAIL)


def _origin_netloc(origin: str) -> str:
    """取 Origin 的 host[:port]，显式默认端口视为同源。"""
    parsed = urlparse(origin)
    netloc = parsed.netloc.strip().lower()
    if parsed.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    elif parsed.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    return netloc


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        USER_SESSION_COOKIE,
        token,
        max_age=int(SESSION_ABSOLUTE_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        path="/",
    )


def _current_user_from_cookie(session: Session, request: Request):
    """有合法 pf_user_session cookie 时返回当前用户，否则 None（不抛错）。"""
    token = request.cookies.get(USER_SESSION_COOKIE)
    if not token:
        return None
    try:
        user, _ = authenticate_session(session, token=token)
    except SessionInvalidError:
        return None
    return user


def _user_response(user) -> UserResponse:
    return UserResponse(id=user.id, username=user.username, role=user.role, display_name=user.display_name)


def _invite_created_response(invite: UserInvite, token: str) -> InviteCreatedResponse:
    return InviteCreatedResponse(
        id=invite.id,
        token=token,
        invite_path=f"/invite/{token}",
        expires_at=invite.expires_at,
        created_by=invite.created_by,
        note=invite.note,
        created_at=invite.created_at,
    )


# ---- 邀请管理（现有 admin-key 管理员） ----


@router.post(
    "/invites",
    response_model=InviteCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_user_invite(payload: InviteCreateRequest, request: Request, session: Session = Depends(get_session)):
    _enforce_csrf(request)
    current_user = _current_user_from_cookie(session, request)
    created_by = current_user.username if current_user is not None else "admin"
    invite, token = create_invite(session, created_by=created_by, note=payload.note)
    return _invite_created_response(invite, token)


@router.get("/invites", response_model=InviteListResponse, dependencies=[Depends(require_admin)])
def list_user_invites(session: Session = Depends(get_session)):
    invites = list_invites(session)
    return InviteListResponse(
        invites=[
            InviteListItem(
                id=invite.id,
                created_by=invite.created_by,
                note=invite.note,
                expires_at=invite.expires_at,
                used_at=invite.used_at,
                used_by=invite.used_by,
                revoked_at=invite.revoked_at,
                created_at=invite.created_at,
            )
            for invite in invites
        ]
    )


@router.delete("/invites/{invite_id}", response_model=InviteRevokeResponse, dependencies=[Depends(require_admin)])
def revoke_user_invite(invite_id: str, request: Request, session: Session = Depends(get_session)):
    _enforce_csrf(request)
    try:
        invite = revoke_invite(session, invite_id)
    except InviteInvalidError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return InviteRevokeResponse(id=invite.id, revoked_at=invite.revoked_at)


# ---- 邀请兑换（匿名，入口限速） ----


@router.post("/invite/redeem", response_model=UserResponse)
def redeem_user_invite(
    payload: InviteRedeemRequest, request: Request, response: Response, session: Session = Depends(get_session)
):
    user_redeem_entry_rate_limiter.hit(client_ip(request))
    _enforce_csrf(request)
    try:
        user, _, session_token = redeem_invite(
            session,
            token=payload.token,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
        )
    except InviteInvalidError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except UsernameTakenError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidUserDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _set_session_cookie(response, session_token)
    return _user_response(user)


# ---- 用户登录 / 登出 / me ----


@router.post("/user/login", response_model=UserResponse)
def user_login(
    payload: UserLoginRequest, request: Request, response: Response, session: Session = Depends(get_session)
):
    user_login_entry_rate_limiter.hit(client_ip(request))
    _enforce_csrf(request)
    try:
        user, _, session_token = login_with_password(session, username=payload.username, password=payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except InvalidUserDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _set_session_cookie(response, session_token)
    return _user_response(user)


@router.post("/user/logout", response_model=OkResponse)
def user_logout(request: Request, response: Response, session: Session = Depends(get_session)):
    _enforce_csrf(request)
    token = request.cookies.get(USER_SESSION_COOKIE)
    if token:
        revoke_user_session(session, token=token)
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
    return OkResponse()


@router.get("/user/me", response_model=UserResponse)
def user_me(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get(USER_SESSION_COOKIE)
    try:
        user, _ = authenticate_session(session, token=token or "")
    except SessionInvalidError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return _user_response(user)
