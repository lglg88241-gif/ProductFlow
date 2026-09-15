"""独立账号身份端点（批次 B 第一批）的请求/响应模型。

与现有 admin-key 会话（schemas/auth.py）完全独立，仅服务 /api/auth/invites、
/api/auth/invite/redeem 和 /api/auth/user/* 三个新增端点组。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from productflow_backend.application.user_accounts import MAX_USERNAME_LENGTH, MIN_PASSWORD_LENGTH


class InviteCreateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class InviteCreatedResponse(BaseModel):
    """创建成功响应：明文 token 只在此出现一次，之后无法再取回。"""

    id: str
    token: str
    invite_path: str
    expires_at: datetime
    created_by: str | None = None
    note: str | None = None
    created_at: datetime


class InviteListItem(BaseModel):
    id: str
    created_by: str | None = None
    note: str | None = None
    expires_at: datetime
    used_at: datetime | None = None
    used_by: str | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class InviteListResponse(BaseModel):
    invites: list[InviteListItem]


class InviteRevokeResponse(BaseModel):
    id: str
    revoked_at: datetime | None = None


class InviteRedeemRequest(BaseModel):
    token: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class UserLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: str
    username: str
    role: str
    display_name: str | None = None


class OkResponse(BaseModel):
    ok: bool = True
