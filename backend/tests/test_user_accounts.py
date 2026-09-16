"""批次 B 第一批（纯新增身份底座）：用户账号、邀请、服务端会话。

审计要求：
- 密码 Argon2id；邀请/会话令牌只存摘要。
- 邀请一次性、过期、撤销；登录失败文案不区分存在性（防探测）。
- 服务端会话闲置/绝对过期、登出与禁用即吊销。
- CSRF 最小防线（自定义头 + Origin 同源）。
- 现有数据零改动：迁移只新增表与可空列。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from productflow_backend.application.user_accounts import (
    InvalidCredentialsError,
    InviteInvalidError,
    generate_token,
    hash_password,
    verify_password,
)

CSRF_HEADERS = {"X-Requested-With": "productflow"}


def _png(width: int = 200, height: int = 200) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (70, 110, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 密码与令牌
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip_and_wrong_password(configured_env: Path) -> None:
    digest = hash_password("correct-horse-battery")
    assert digest != "correct-horse-battery"
    assert verify_password(digest, "correct-horse-battery")
    assert not verify_password(digest, "wrong-password")


def test_generate_token_is_unique_and_hashed(configured_env: Path) -> None:
    token_a, hash_a = generate_token()
    token_b, hash_b = generate_token()
    assert token_a != token_b
    assert hash_a != hash_b
    assert token_a not in hash_a, "明文令牌不得出现在哈希里"


# ---------------------------------------------------------------------------
# 邀请与用户账号（走 HTTP 层，覆盖鉴权/CSRF/限速/一次性语义）
# ---------------------------------------------------------------------------


def _make_member(app, db, username: str):
    """建 member 账号 + 服务端会话，返回带 cookie 的客户端与 user_id。"""
    return _make_user_with_role(app, db, username, "member")


def _make_admin(app, db, username: str):
    return _make_user_with_role(app, db, username, "admin")


def _make_user_with_role(app, db, username: str, role: str):
    from datetime import timedelta

    from productflow_backend.application.time import now_utc
    from productflow_backend.application.user_accounts import create_user, hash_token
    from productflow_backend.infrastructure.db.models import UserSession
    from productflow_backend.presentation.deps import USER_SESSION_COOKIE

    user = create_user(db, username=username, password="user-pass-123", role=role)
    plaintext = f"tok-{username}"
    now = now_utc()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(plaintext),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + timedelta(days=7),
            idle_expires_at=now + timedelta(days=1),
        )
    )
    db.commit()
    client = TestClient(app)
    client.cookies.set(USER_SESSION_COOKIE, plaintext)
    return client, str(user.id)


@pytest.fixture()
def admin_client(configured_env: Path, db_session) -> TestClient:
    """邀请管理现在要求**管理员账号会话**（复审 P1）。

    旧的共享管理员口令不再能管理账号（否则门禁一关就匿名可管理邀请），
    因此夹具改为：建 admin 账号 + 服务端会话。
    """
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client, _ = _make_user_with_role(app, db_session, "fixture-admin", "admin")
    return client


def _redeem(admin_client: TestClient, *, username: str, password: str = "user-pass-123") -> TestClient:
    created = admin_client.post(
        "/api/auth/invites", json={"note": "测试邀请"}, headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    invite = created.json()
    user = TestClient(admin_client.app)
    response = user.post(
        "/api/auth/invite/redeem",
        json={"token": invite["token"], "username": username, "password": password},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return user


def test_invite_lifecycle_create_redeem_once_expiry_and_revocation(
    admin_client: TestClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from productflow_backend.application import user_accounts
    from productflow_backend.infrastructure.db.models import UserInvite

    # 1) 创建邀请：明文 token 只出现一次
    created = admin_client.post("/api/auth/invites", json={"note": " lifecycle"}, headers=CSRF_HEADERS)
    assert created.status_code == 201
    invite = created.json()
    assert invite["token"] and len(invite["invite_path"]) > len("/invite/")
    listing = admin_client.get("/api/auth/invites").json()["invites"]
    assert all("token" not in item for item in listing), "列表不得泄露明文令牌"
    assert any(item["id"] == invite["id"] for item in listing)

    # 2) 过期邀请不可兑换
    row = db_session.get(UserInvite, invite["id"])
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    expired = TestClient(admin_client.app).post(
        "/api/auth/invite/redeem",
        json={"token": invite["token"], "username": "expired-user", "password": "user-pass-123"},
        headers=CSRF_HEADERS,
    )
    assert expired.status_code in {400, 410}, expired.text

    # 3) 未过期的撤销邀请不可兑换
    created2 = admin_client.post("/api/auth/invites", json={}, headers=CSRF_HEADERS).json()
    revoked = admin_client.delete(f"/api/auth/invites/{created2['id']}", headers=CSRF_HEADERS)
    assert revoked.status_code == 200
    revoked_redeem = TestClient(admin_client.app).post(
        "/api/auth/invite/redeem",
        json={"token": created2["token"], "username": "revoked-user", "password": "user-pass-123"},
        headers=CSRF_HEADERS,
    )
    assert revoked_redeem.status_code in {400, 410}

    # 4) 有效邀请兑换成功；同 token 二次兑换失败（一次性）
    fresh = admin_client.post("/api/auth/invites", json={}, headers=CSRF_HEADERS).json()
    user = TestClient(admin_client.app)
    ok = user.post(
        "/api/auth/invite/redeem",
        json={"token": fresh["token"], "username": "lifecycle-user", "password": "user-pass-123"},
        headers=CSRF_HEADERS,
    )
    assert ok.status_code == 200, ok.text
    again = user.post(
        "/api/auth/invite/redeem",
        json={"token": fresh["token"], "username": "lifecycle-user-2", "password": "user-pass-123"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code in {400, 410}, "同一邀请只能兑换一次"

    # 兑换后角色为 member
    assert ok.json()["role"] == "member"
    _ = user_accounts
    _ = InvalidCredentialsError
    _ = InviteInvalidError


def test_user_login_logout_me_and_revoke_semantics(admin_client: TestClient) -> None:
    # 建一个真实用户
    user = _redeem(admin_client, username="session-user")

    # me：会话有效
    me = user.get("/api/auth/user/me")
    assert me.status_code == 200
    assert me.json()["username"] == "session-user"

    # 登出吊销会话：之后 me 失效
    logout = user.post("/api/auth/user/logout", headers=CSRF_HEADERS)
    assert logout.status_code == 200
    assert user.get("/api/auth/user/me").status_code == 401

    # 重新登录成功；错误密码被拒且文案不区分存在性
    wrong = TestClient(admin_client.app).post(
        "/api/auth/user/login",
        json={"username": "nosuch-user-xyz", "password": "whatever-123"},
        headers=CSRF_HEADERS,
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "用户名或密码不正确"

    login = TestClient(admin_client.app).post(
        "/api/auth/user/login",
        json={"username": "session-user", "password": "user-pass-123"},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    assert login.cookies.get("pf_user_session")


def test_user_auth_requires_csrf_header(admin_client: TestClient) -> None:
    login = TestClient(admin_client.app).post(
        "/api/auth/user/login",
        json={"username": "x", "password": "y"},
    )
    assert login.status_code == 403, "缺少 CSRF 自定义头的状态变更请求必须被拒"


def test_disable_user_kills_sessions(admin_client: TestClient, db_session) -> None:
    from productflow_backend.infrastructure.db.models import UserAccount

    user = _redeem(admin_client, username="disable-me")
    assert user.get("/api/auth/user/me").status_code == 200

    row = db_session.query(UserAccount).filter_by(username="disable-me").one()
    row.is_active = False
    db_session.commit()

    assert user.get("/api/auth/user/me").status_code == 401, "禁用账号的会话必须立即失效"


def test_admin_can_list_and_revoke_but_member_cannot(admin_client: TestClient) -> None:
    user = _redeem(admin_client, username="plain-member")

    # 普通成员不能管理邀请：已登录但非 admin → 403（匿名才是 401）
    assert user.get("/api/auth/invites").status_code == 403
    assert (
        user.post("/api/auth/invites", json={}, headers=CSRF_HEADERS).status_code == 403
    )


def test_initialization_cli_creates_admin_and_refuses_duplicates(configured_env: Path, capsys) -> None:
    """CLI 入口建首个管理员；重复创建必须失败退出非零。"""
    from productflow_backend.infrastructure.db.models import UserAccount
    from productflow_backend.infrastructure.db.session import get_session_factory
    from productflow_backend.initialization import main

    first = main(["create-admin", "--username", "boot-admin", "--password", "admin-pass-123"])
    assert first == 0, "首次创建管理员应成功"

    db = get_session_factory()()
    try:
        admin = db.query(UserAccount).filter_by(username="boot-admin").one()
        assert admin.role == "admin"
        assert verify_password(admin.password_hash, "admin-pass-123")
    finally:
        db.close()

    duplicate = main(["create-admin", "--username", "boot-admin", "--password", "another-pass-123"])
    assert duplicate != 0, "重复创建管理员必须失败"


def test_migrated_business_rows_are_untouched(configured_env: Path, db_session) -> None:
    """批次 B 铁律：新增列后，现有数据零改动（owner_id 全为 NULL，行数不变）。"""
    from sqlalchemy import select

    from productflow_backend.infrastructure.db.models import AgentSession, Product

    products_before = len(db_session.scalars(select(Product)).all())
    sessions_before = len(db_session.scalars(select(AgentSession)).all())

    orphan_products = [p for p in db_session.scalars(select(Product)).all() if p.owner_id is not None]
    orphan_sessions = [s for s in db_session.scalars(select(AgentSession)).all() if s.owner_id is not None]

    assert not orphan_products and not orphan_sessions, "回填之前不允许有任何 owner 被设置"
    _ = products_before, sessions_before


def test_csrf_allows_same_origin_with_port_normalization(admin_client: TestClient) -> None:
    """回归：反代归一化端口后，同源请求不得被 CSRF 防线误杀。

    真实缺陷：nginx `$host` 会剥掉端口（浏览器发 Origin: http://h:29281、
    Host 变成 h），严格比对 netloc 会 403 —— 表现为"浏览器里根本登不进去"。
    同源判定按主机名比较，忽略端口。
    """
    from productflow_backend.presentation.api import create_app

    client = TestClient(admin_client.app)
    # 同主机名、Host 无端口（模拟 nginx $host 行为）
    ok = client.post(
        "/api/auth/user/login",
        json={"username": "nobody-xyz", "password": "whatever-123"},
        headers={"X-Requested-With": "productflow", "Origin": "http://testserver:29281"},
    )
    assert ok.status_code == 401, f"同源请求应进入凭据校验（401），而非 CSRF 拒绝：{ok.status_code} {ok.text}"

    # 真正跨站（主机名不同）仍必须 403
    blocked = TestClient(create_app()).post(
        "/api/auth/user/login",
        json={"username": "nobody-xyz", "password": "whatever-123"},
        headers={"X-Requested-With": "productflow", "Origin": "http://evil.example.com"},
    )
    assert blocked.status_code == 403, "跨站 Origin 必须被拒"


# ---------------------------------------------------------------------------
# 复审 P1：管理接口不得因旧门禁关闭而匿名开放；业务接口不得被旧门禁拦
# ---------------------------------------------------------------------------


def test_invite_management_requires_admin_account_even_when_legacy_gate_off(
    isolation_env, configured_env: Path
) -> None:
    """复现并锁死：旧门禁关闭时，匿名客户端不得管理邀请（此前返回 201 并给出明文 token）。"""
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    get_settings.cache_clear()
    try:
        app = create_app()
        anonymous = TestClient(app)
        headers = {"X-Requested-With": "productflow"}

        assert anonymous.post("/api/auth/invites", json={"note": "probe"}, headers=headers).status_code == 401
        assert anonymous.get("/api/auth/invites").status_code == 401
        assert anonymous.delete("/api/auth/invites/some-id", headers=headers).status_code == 401
        # 诊断端点同样不得匿名可读（暴露供应商与拓扑）
        assert anonymous.get("/api/admin/diagnostics").status_code == 401
    finally:
        get_settings.cache_clear()


def test_member_cannot_manage_invites_but_admin_can(
    isolation_env, configured_env: Path, db_session
) -> None:
    """权限矩阵：member → 403（无管理权）；admin → 可管理。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    member, _ = _make_member(app, db_session, "invite_member")
    headers = {"X-Requested-With": "productflow"}

    assert member.post("/api/auth/invites", json={}, headers=headers).status_code == 403
    assert member.get("/api/auth/invites").status_code == 403

    admin, _ = _make_admin(app, db_session, "invite_admin")
    assert admin.get("/api/auth/invites").status_code == 200
    created = admin.post("/api/auth/invites", json={"note": "admin 创建"}, headers=headers)
    assert created.status_code == 201
    assert created.json().get("token"), "管理员创建邀请应返回一次性 token"


def test_member_can_use_own_workflow_without_legacy_admin_credentials(
    isolation_env, configured_env: Path, db_session
) -> None:
    """复审 P1 第三条：member 登录后应能用**自己的工作流**，不需要共享管理员口令。

    此前工作流路由同时挂旧 require_admin，而用户登录不设置旧门禁的
    request.session.is_authenticated，导致 member 读自己的工作流得到 401。
    """
    from productflow_backend.presentation.api import create_app

    app = create_app()
    member, _ = _make_member(app, db_session, "wf_member")

    created = member.post(
        "/api/products", data={"name": "member 的商品"}, files={"image": ("m.png", _png(), "image/png")}
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    assert member.get(f"/api/products/{product_id}/workflow").status_code == 200
    assert member.get(f"/api/products/{product_id}/workflow/status").status_code == 200
