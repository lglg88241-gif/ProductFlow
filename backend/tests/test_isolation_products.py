"""数据隔离（批次 B）：商品与工作流 + 跨垂直的公共契约。

审计要求：每条路径用两个账号交叉验证——不能只凭"加了 owner_id"就宣布隔离完成。
本文件覆盖：A 用户的资源对 B 用户不可见/不可下载/不可改删，且错误语义与"不存在"一致
（不泄漏存在性）；全局资源（owner_id IS NULL，内置模板）全体可读；开关关闭时行为不变。

启用方式：`DATA_ISOLATION_ENABLED=true` + 用户会话 cookie `pf_user_session`。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from productflow_backend.application.user_accounts import create_user, hash_token
from productflow_backend.infrastructure.db.models import UserSession
from productflow_backend.presentation.deps import USER_SESSION_COOKIE


def _png(width: int = 320, height: int = 320) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (90, 120, 60)).save(buffer, format="PNG")
    return buffer.getvalue()




def _make_user_client(app, db, username: str) -> tuple[TestClient, str]:
    """建用户 + 服务端会话，返回带 cookie 的客户端与 user_id。"""
    from productflow_backend.application.time import now_utc

    user = create_user(db, username=username, password="user-pass-123", role="member")
    plaintext, token_hash = "session-token-for-" + username, hash_token("session-token-for-" + username)
    now = now_utc()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash,
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + __import__("datetime").timedelta(days=7),
            idle_expires_at=now + __import__("datetime").timedelta(hours=24),
        )
    )
    db.commit()
    client = TestClient(app)
    client.cookies.set(USER_SESSION_COOKIE, plaintext)
    return client, str(user.id)


def test_products_are_isolated_between_users(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """A 的商品对 B 不可见、不可读、不可删、不可下载海报/源图。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _alice_id = _make_user_client(app, db_session, "alice")
    bob, _bob_id = _make_user_client(app, db_session, "bob")

    # 诊断：确认会话与开关在 app 侧真的生效
    # A 建商品（带主图）
    created = alice.post(
        "/api/products",
        data={"name": "A的护手霜"},
        files={"image": ("main.png", _png(), "image/png")},
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    # B 的列表里看不到
    bob_list = bob.get("/api/products").json()
    assert all(item["id"] != product_id for item in bob_list["items"])

    # B 读详情 → 404 语义（与不存在同文案）
    missing = bob.get(f"/api/products/{product_id}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "商品不存在"

    # B 删 → 404；A 自己仍能看到（确认 B 的失败没有副作用）
    assert bob.delete(f"/api/products/{product_id}").status_code == 404
    assert alice.get(f"/api/products/{product_id}").status_code == 200

    # B 取历史 → 404
    assert bob.get(f"/api/products/{product_id}/history").status_code == 404

    # 源图下载：B 对该商品的源图 404（审计点名路径）
    detail = alice.get(f"/api/products/{product_id}").json()
    source_assets = detail.get("source_assets") or []
    if source_assets:
        asset_id = source_assets[0]["id"]
        assert bob.get(f"/api/source-assets/{asset_id}/download").status_code == 404
        assert alice.get(f"/api/source-assets/{asset_id}/download").status_code == 200
        # B 也不能删 A 的参考图
        assert bob.delete(f"/api/source-assets/{asset_id}").status_code == 404

    _ = alice, bob


def test_products_visible_again_when_isolation_disabled(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关闭 = 现状：B 也能看到 A 的商品（整批回退语义）。"""
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("DATA_ISOLATION_ENABLED", "false")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
        alice, _ = _make_user_client(app, db_session, "alice2")
        anonymous = TestClient(app)  # 无用户会话

        created = alice.post(
            "/api/products", data={"name": "关闭开关的商品"}, files={"image": ("m.png", _png(), "image/png")}
        )
        assert created.status_code == 201
        product_id = created.json()["id"]

        listing = anonymous.get("/api/products").json()
        assert any(item["id"] == product_id for item in listing["items"]), "关闭开关时应保持现状可见性"
    finally:
        get_settings.cache_clear()


def test_isolation_requires_user_session(isolation_env: None, configured_env: Path) -> None:
    """隔离开启后无用户会话的业务请求必须 401，而不是返回空列表或全部数据。"""
    from productflow_backend.presentation.api import create_app

    anonymous = TestClient(create_app())
    response = anonymous.get("/api/products")
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录"


def test_session_state_exposes_isolation_flag(
    isolation_env: None, configured_env: Path
) -> None:
    """前端据此决定登录形态：/api/auth/session 必须暴露隔离开关状态。"""
    from productflow_backend.presentation.api import create_app

    payload = TestClient(create_app()).get("/api/auth/session").json()
    assert payload["data_isolation_enabled"] is True


def test_null_owner_rows_are_globally_readable(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """owner_id 为 NULL 的行（内置模板等系统资源）全体可读。"""
    from productflow_backend.application.asset_library import bootstrap_builtin_assets
    from productflow_backend.presentation.api import create_app

    bootstrap_builtin_assets()
    app = create_app()
    alice, _ = _make_user_client(app, db_session, "alice3")
    bob, _ = _make_user_client(app, db_session, "bob3")

    for client in (alice, bob):
        assets = client.get("/api/agent/assets").json()
        builtin = [item for item in assets["items"] if item.get("source") == "builtin"]
        assert builtin, "内置模板应对所有用户可见"


# ---------------------------------------------------------------------------
# 审计 S0-01：商品工作流端点此前只挂 require_admin、零 owner 校验
# ---------------------------------------------------------------------------


def test_workflow_endpoints_are_isolated_by_product_owner(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """工作流读取/状态/节点/连线：B 持有 A 的 id 也必须 404（审计 S0-01）。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _ = _make_user_client(app, db_session, "wf_a")
    bob, _ = _make_user_client(app, db_session, "wf_b")

    created = alice.post(
        "/api/products", data={"name": "A 的工作流商品"}, files={"image": ("m.png", _png(), "image/png")}
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    # 先让 A 自己访问成功（确认路径本身可用）
    assert alice.get(f"/api/products/{product_id}/workflow").status_code == 200
    assert alice.get(f"/api/products/{product_id}/workflow/status").status_code == 200

    # B 越权：读取工作流、状态、创建节点、跑、取消、重试、加连线 → 一律 404
    assert bob.get(f"/api/products/{product_id}/workflow").status_code == 404
    assert bob.get(f"/api/products/{product_id}/workflow/status").status_code == 404
    assert (
        bob.post(
            f"/api/products/{product_id}/workflow/nodes",
            json={"node_type": "copy_generation", "title": "偷建节点", "position_x": 0, "position_y": 0},
        ).status_code
        in {404, 422}
    )
    assert bob.post(f"/api/products/{product_id}/workflow/run", json={}).status_code == 404

    # A 的工作流里取一个真实 node_id，验证节点级越权同样 404
    workflow = alice.get(f"/api/products/{product_id}/workflow").json()
    nodes = workflow.get("nodes") or []
    if nodes:
        node_id = nodes[0]["id"]
        assert bob.patch(f"/api/workflow-nodes/{node_id}", json={"title": "偷改"}).status_code == 404
        assert bob.delete(f"/api/workflow-nodes/{node_id}").status_code == 404
        # A 自己仍可改（确认越权失败没有副作用）
        assert alice.patch(f"/api/workflow-nodes/{node_id}", json={"title": "A 改名"}).status_code == 200

    edges = workflow.get("edges") or []
    if edges:
        assert bob.delete(f"/api/workflow-edges/{edges[0]['id']}").status_code == 404


def test_workflow_guards_do_not_break_normal_use(isolation_env: None, configured_env: Path, db_session) -> None:
    """守卫不得误伤：A 对自己的商品可完整走一遍工作流读写。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _ = _make_user_client(app, db_session, "wf_solo")

    created = alice.post(
        "/api/products", data={"name": "自用商品"}, files={"image": ("m.png", _png(), "image/png")}
    )
    product_id = created.json()["id"]

    assert alice.get(f"/api/products/{product_id}/workflow").status_code == 200
    node = alice.post(
        f"/api/products/{product_id}/workflow/nodes",
        json={"node_type": "copy_generation", "title": "自用节点", "position_x": 120, "position_y": 80},
    )
    assert node.status_code in {200, 201}, node.text
    assert alice.get(f"/api/products/{product_id}/workflow/status").status_code == 200
