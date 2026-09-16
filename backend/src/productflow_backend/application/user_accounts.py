"""独立账号底座（批次 B 第一批，纯新增）：密码哈希、一次性邀请、服务端会话。

边界约束（与 docs/BATCH_B_PLAN.md §3 对应）：
- 只新增能力，不改任何现有端点/会话行为；现有 admin-key 门禁与本模块无关；
- 邀请与会话令牌只存 sha256 哈希，明文只在创建/签发响应里出现一次；
- 登录失败一律同一句"用户名或密码不正确"，不区分用户名是否存在（含时序均衡）；
- 会话双过期：闲置 24h（滑动续期 last_seen）、绝对 7d；登出/禁用立即吊销；
- 邀请兑换的原子性由"同一事务内的条件 UPDATE 抢占 + 唯一约束"保证：
  并发兑换同一 token 只有一个事务能把 used_at 从 NULL 改为当前时刻。

所有函数接收调用方持有的 SQLAlchemy ``Session`` 并自行 ``commit()``，
与应用层其余服务（gallery 等）保持一致。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from productflow_backend.infrastructure.db.models import UserAccount, UserInvite, UserSession

# ---- 模块顶层常量（部署调整直接改这里；测试通过参数注入伪造时间与时长） ----
SESSION_IDLE_LIFETIME = timedelta(hours=24)
SESSION_ABSOLUTE_LIFETIME = timedelta(days=7)
INVITE_DEFAULT_TTL = timedelta(hours=24)
# 产品决定（2026-09-16）：本地验收场景密码最低 6 位
MIN_PASSWORD_LENGTH = 6
MAX_USERNAME_LENGTH = 64
ROLES = ("admin", "member")
DEFAULT_ROLE = "member"

# 登录失败统一文案：不区分"用户不存在"与"密码错误"
INVALID_CREDENTIALS_MESSAGE = "用户名或密码不正确"
SESSION_INVALID_MESSAGE = "会话已失效，请重新登录"


class UserAccountsError(Exception):
    """独立账号域的统一错误基类。"""


class InvalidCredentialsError(UserAccountsError):
    """用户名或密码不正确（统一文案，不区分存在性）。"""


class UsernameTakenError(UserAccountsError):
    """用户名已被占用。"""


class InviteInvalidError(UserAccountsError):
    """邀请不存在、已过期、已被使用或已撤销。"""


class SessionInvalidError(UserAccountsError):
    """会话不存在、已过期或已吊销。"""


class InvalidUserDataError(UserAccountsError):
    """用户名/密码/角色等输入不合法。"""


# ---- 密码哈希（argon2id） ----

_password_hasher = PasswordHasher()  # argon2 默认参数即 argon2id
_dummy_hash_lock = threading.Lock()
_dummy_password_hash: str | None = None


def hash_password(password: str) -> str:
    """生成 argon2id 哈希（随机盐，含算法参数前缀）。"""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码；任何校验异常一律按不匹配处理（坏哈希也不例外）。"""
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError, ValueError, TypeError):
        return False


def _timing_equalized_failure(password: str) -> None:
    """用户名不存在时仍执行一次真实 argon2 校验，抹平响应时序差。"""
    global _dummy_password_hash
    if _dummy_password_hash is None:
        with _dummy_hash_lock:
            if _dummy_password_hash is None:
                _dummy_password_hash = _password_hasher.hash("productflow-dummy-account-timing")
    try:
        _password_hasher.verify(_dummy_password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        pass


# ---- 令牌 ----


def hash_token(token: str) -> str:
    """令牌的库内形态：sha256 hex（64 字符）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """生成一次性令牌：返回 (明文, sha256 哈希)。明文只在此刻出现一次。"""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext)


# ---- 基础校验与时间 ----


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite 会丢掉时区读回 naive datetime；统一按 UTC 对齐后比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _validate_username(username: str) -> str:
    normalized = (username or "").strip()
    if not normalized:
        raise InvalidUserDataError("用户名不能为空")
    if len(normalized) > MAX_USERNAME_LENGTH:
        raise InvalidUserDataError(f"用户名长度不能超过 {MAX_USERNAME_LENGTH} 个字符")
    return normalized


def _validate_password(password: str) -> None:
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidUserDataError(f"密码长度至少 {MIN_PASSWORD_LENGTH} 个字符")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise InvalidUserDataError(f"角色必须是以下之一: {', '.join(ROLES)}")


# ---- 用户 ----


def get_user_by_username(session: Session, username: str) -> UserAccount | None:
    return session.scalar(select(UserAccount).where(UserAccount.username == (username or "").strip()))


def get_user_by_id(session: Session, user_id: str) -> UserAccount | None:
    return session.get(UserAccount, user_id)


def has_any_admin(session: Session) -> bool:
    """是否已存在任意管理员（CLI create-admin 的幂等闸门）。"""
    return session.scalar(select(UserAccount.id).where(UserAccount.role == "admin").limit(1)) is not None


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: str = DEFAULT_ROLE,
    display_name: str | None = None,
    now: datetime | None = None,
) -> UserAccount:
    """创建账号；用户名冲突抛 UsernameTakenError。"""
    _validate_role(role)
    normalized_username = _validate_username(username)
    _validate_password(password)
    if get_user_by_username(session, normalized_username) is not None:
        raise UsernameTakenError("用户名已存在")
    user = UserAccount(
        username=normalized_username,
        password_hash=hash_password(password),
        role=role,
        display_name=(display_name or None),
        is_active=True,
        created_at=now or datetime.now(UTC),
    )
    session.add(user)
    session.commit()
    return user


# ---- 邀请 ----


def create_invite(
    session: Session,
    *,
    created_by: str | None = None,
    note: str | None = None,
    ttl: timedelta = INVITE_DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[UserInvite, str]:
    """创建一次性邀请，返回 (invite, 明文 token)。默认 24 小时有效。"""
    current = now or datetime.now(UTC)
    token, token_hash = generate_token()
    invite = UserInvite(
        token_hash=token_hash,
        created_by=created_by,
        note=note,
        expires_at=current + ttl,
        created_at=current,
    )
    session.add(invite)
    session.commit()
    return invite, token


def list_invites(session: Session) -> Sequence[UserInvite]:
    """全部邀请，按创建时间倒序（不含明文 token——库里根本没有）。"""
    return session.scalars(select(UserInvite).order_by(UserInvite.created_at.desc(), UserInvite.id)).all()


def get_invite(session: Session, invite_id: str) -> UserInvite | None:
    return session.get(UserInvite, invite_id)


def revoke_invite(session: Session, invite_id: str, now: datetime | None = None) -> UserInvite:
    """撤销邀请：已使用/已撤销的邀请拒绝或幂等返回。"""
    invite = session.get(UserInvite, invite_id)
    if invite is None:
        raise InviteInvalidError("邀请不存在")
    if invite.used_at is not None:
        raise InviteInvalidError("邀请已被使用，无法撤销")
    if invite.revoked_at is None:
        invite.revoked_at = now or datetime.now(UTC)
        session.commit()
    return invite


def _redeemable_invite(session: Session, token: str, current: datetime) -> UserInvite:
    """按明文 token 找出可兑换的邀请；任何失败统一抛 InviteInvalidError。"""
    if not token:
        raise InviteInvalidError("邀请链接无效、已过期或已被使用")
    invite = session.scalar(select(UserInvite).where(UserInvite.token_hash == hash_token(token)))
    if invite is None or invite.used_at is not None or invite.revoked_at is not None:
        raise InviteInvalidError("邀请链接无效、已过期或已被使用")
    if _as_aware_utc(invite.expires_at) <= current:
        raise InviteInvalidError("邀请链接无效、已过期或已被使用")
    return invite


def _claim_invite_atomic(session: Session, invite_id: str, user_id: str, current: datetime) -> bool:
    """条件 UPDATE 抢占兑换权：并发下只有一个事务能把 used_at 置为非 NULL。

    WHERE 同时校验 used_at/revoked_at/expires_at，因此即使两个事务都通过了
    前面的读检查，也只有一个 UPDATE 能命中；另一边 rowcount=0 → 回滚。
    """
    result = session.execute(
        update(UserInvite)
        .where(
            UserInvite.id == invite_id,
            UserInvite.used_at.is_(None),
            UserInvite.revoked_at.is_(None),
            UserInvite.expires_at > current,
        )
        .values(used_at=current, used_by=user_id)
        # 与 image_sessions 的惯例一致：SQLite 下会话内对象属性是 naive datetime，
        # 与 aware 参数做客户端求值比较会 TypeError——比较交给数据库执行。
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount and result.rowcount > 0)


def _new_user_session_row(
    user_id: str,
    current: datetime,
    idle_lifetime: timedelta,
    absolute_lifetime: timedelta,
) -> tuple[UserSession, str]:
    """构造一条已持有真实令牌哈希的会话行，返回 (row, 明文 token)。"""
    token, token_hash = generate_token()
    row = UserSession(
        user_id=user_id,
        token_hash=token_hash,
        created_at=current,
        last_seen_at=current,
        absolute_expires_at=current + absolute_lifetime,
        idle_expires_at=current + idle_lifetime,
    )
    return row, token


def redeem_invite(
    session: Session,
    *,
    token: str,
    username: str,
    password: str,
    display_name: str | None = None,
    now: datetime | None = None,
    idle_lifetime: timedelta = SESSION_IDLE_LIFETIME,
    absolute_lifetime: timedelta = SESSION_ABSOLUTE_LIFETIME,
) -> tuple[UserAccount, UserSession, str]:
    """兑换邀请：创建 member 用户并签发服务端会话。

    返回 (user, user_session, 会话明文 token)。兑换检查失败抛 InviteInvalidError；
    用户名冲突抛 UsernameTakenError。抢占与建户在同一事务里，commit 失败即整体回滚。
    """
    current = now or datetime.now(UTC)
    invite = _redeemable_invite(session, token, current)
    normalized_username = _validate_username(username)
    _validate_password(password)
    if get_user_by_username(session, normalized_username) is not None:
        raise UsernameTakenError("用户名已存在")
    user = UserAccount(
        username=normalized_username,
        password_hash=hash_password(password),
        role=DEFAULT_ROLE,
        display_name=(display_name or None),
        is_active=True,
        created_at=current,
    )
    session.add(user)
    session.flush()  # 取 user.id，同时让用户名唯一约束在本事务内尽早暴露
    if not _claim_invite_atomic(session, invite.id, user.id, current):
        session.rollback()
        raise InviteInvalidError("邀请链接无效、已过期或已被使用")
    user_session, session_token = _new_user_session_row(user.id, current, idle_lifetime, absolute_lifetime)
    session.add(user_session)
    session.commit()
    return user, user_session, session_token


# ---- 登录 / 会话校验 / 登出 ----


def login_with_password(
    session: Session,
    *,
    username: str,
    password: str,
    now: datetime | None = None,
    idle_lifetime: timedelta = SESSION_IDLE_LIFETIME,
    absolute_lifetime: timedelta = SESSION_ABSOLUTE_LIFETIME,
) -> tuple[UserAccount, UserSession, str]:
    """用户名密码登录：验证后签发服务端会话，返回 (user, user_session, 明文 token)。

    所有失败（用户不存在/密码错误/账号停用）统一抛 InvalidCredentialsError，
    文案恒为"用户名或密码不正确"。
    """
    current = now or datetime.now(UTC)
    user = get_user_by_username(session, username)
    if user is None:
        _timing_equalized_failure(password)
        raise InvalidCredentialsError(INVALID_CREDENTIALS_MESSAGE)
    if not verify_password(user.password_hash, password):
        raise InvalidCredentialsError(INVALID_CREDENTIALS_MESSAGE)
    if not user.is_active:
        raise InvalidCredentialsError(INVALID_CREDENTIALS_MESSAGE)
    user_session, session_token = _new_user_session_row(user.id, current, idle_lifetime, absolute_lifetime)
    session.add(user_session)
    session.commit()
    return user, user_session, session_token


def authenticate_session(
    session: Session,
    *,
    token: str,
    now: datetime | None = None,
    idle_lifetime: timedelta = SESSION_IDLE_LIFETIME,
) -> tuple[UserAccount, UserSession]:
    """校验会话 token：吊销/绝对过期/闲置过期/账号停用一律 SessionInvalidError。

    命中即滑动续期：last_seen_at 推进到当前时刻，idle_expires_at 重新计算。
    """
    current = now or datetime.now(UTC)
    if not token:
        raise SessionInvalidError(SESSION_INVALID_MESSAGE)
    user_session = session.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if user_session is None or user_session.revoked_at is not None:
        raise SessionInvalidError(SESSION_INVALID_MESSAGE)
    if _as_aware_utc(user_session.absolute_expires_at) <= current:
        raise SessionInvalidError(SESSION_INVALID_MESSAGE)
    if _as_aware_utc(user_session.idle_expires_at) <= current:
        raise SessionInvalidError(SESSION_INVALID_MESSAGE)
    user = session.get(UserAccount, user_session.user_id)
    if user is None or not user.is_active:
        raise SessionInvalidError(SESSION_INVALID_MESSAGE)
    # 滑动续期：闲置窗口从本次访问重新起算（绝对到期时间不动）
    user_session.last_seen_at = current
    user_session.idle_expires_at = current + idle_lifetime
    session.commit()
    return user, user_session


def revoke_user_session(session: Session, *, token: str, now: datetime | None = None) -> bool:
    """按明文 token 吊销会话（登出）。返回是否找到并吊销了会话。"""
    if not token:
        return False
    user_session = session.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if user_session is None or user_session.revoked_at is not None:
        return False
    user_session.revoked_at = now or datetime.now(UTC)
    session.commit()
    return True
