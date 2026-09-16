"""数据隔离（批次 B）：图片会话、画廊、生成队列。

审计要求逐路径双账号交叉验证，尤其**下载与预览端点**（图片资产是数据私有最容易被
绕过的面）。本文件验证：B 用户看不到、打不开、改不了、删不掉 A 的图片会话与其资产。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from productflow_backend.infrastructure.db.session import get_session_factory
from tests.test_isolation_products import _make_user_client


def test_image_sessions_are_isolated_between_users(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """A 的图片会话与资产：B 列表不可见、详情/状态 404、下载 404、删除 404。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _ = _make_user_client(app, db_session, "isoa")
    bob, _ = _make_user_client(app, db_session, "isob")

    created = alice.post("/api/image-sessions", json={"title": "A 的会话"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # B 的列表看不到
    listing = bob.get("/api/image-sessions").json()
    items = listing.get("items", listing if isinstance(listing, list) else [])
    assert all(item["id"] != session_id for item in items), "B 的会话列表里出现了 A 的会话"

    # B 读详情/状态 → 404（与不存在同文案）
    assert bob.get(f"/api/image-sessions/{session_id}").status_code == 404
    assert bob.get(f"/api/image-sessions/{session_id}/status").status_code == 404
    # B 改标题 → 404
    assert bob.patch(f"/api/image-sessions/{session_id}", json={"title": "偷改"}).status_code == 404
    # B 删 → 404，且 A 的会话仍在
    assert bob.delete(f"/api/image-sessions/{session_id}").status_code == 404
    assert alice.get(f"/api/image-sessions/{session_id}").status_code == 200


def test_image_session_asset_download_is_isolated(
    isolation_env: None, configured_env: Path, db_session, install_scripted_llm
) -> None:
    """资产下载端点：B 拿 A 的 asset_id 必须 404（审计点名路径）。"""
    from productflow_backend.application.image_sessions import (
        create_image_session,
        create_image_session_generation_task,
        execute_image_session_generation_task,
    )
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "isoa2")
    bob, bob_id = _make_user_client(app, db_session, "isob2")

    # 直接在服务层造一个属于 A 的会话与已完成资产（绕开生图供应商）
    from productflow_backend.application.user_accounts import create_user

    _ = create_user(db_session, username="isoa3", password="user-pass-123")  # 保持 DB 有活跃用户
    db = get_session_factory()()
    try:
        session_obj = create_image_session(db, title="A 的图片会话", owner_id=alice_id)
        task = create_image_session_generation_task(
            db, image_session_id=session_obj.id, prompt="海报", size="1024x1024"
        )
        install_scripted_llm([])  # 走内联队列完成生图
        execute_image_session_generation_task(task.task.id)
        db.expire_all()
        from productflow_backend.application.image_sessions import get_image_session_detail

        detail = get_image_session_detail(db, session_obj.id)
        assets = [r.generated_asset for r in detail.rounds if r.generated_asset is not None]
        assert assets, "测试前置失败：没有生成出资产"
        asset_id = assets[0].id
    finally:
        db.close()

    # A 能下载自己的资产；B 拿同一个 asset_id 必须 404
    assert alice.get(f"/api/image-session-assets/{asset_id}/download").status_code == 200
    cross = bob.get(f"/api/image-session-assets/{asset_id}/download")
    assert cross.status_code == 404, f"B 下载到了 A 的资产: {cross.status_code}"
    _ = bob_id


def test_gallery_entries_are_isolated(
    isolation_env: None, configured_env: Path, db_session, install_scripted_llm
) -> None:
    """画廊：条目归属沿其图片会话派生——A 投的画廊条目 B 看不到；B 无权对 A 的资产投画廊。"""
    from productflow_backend.application.gallery import list_gallery_entries, save_generated_asset_to_gallery
    from productflow_backend.application.image_sessions import (
        create_image_session,
        create_image_session_generation_task,
        execute_image_session_generation_task,
        get_image_session_detail,
    )
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "isoa4")
    bob, bob_id = _make_user_client(app, db_session, "isob4")

    db = get_session_factory()()
    try:
        # A 生成一张图并投画廊
        session_obj = create_image_session(db, title="A 的画廊来源", owner_id=alice_id)
        task = create_image_session_generation_task(
            db, image_session_id=session_obj.id, prompt="海报", size="1024x1024"
        )
        install_scripted_llm([])
        execute_image_session_generation_task(task.task.id)
        db.expire_all()
        detail = get_image_session_detail(db, session_obj.id)
        assets = [r.generated_asset for r in detail.rounds if r.generated_asset is not None]
        assert assets, "测试前置失败：没有生成出资产"
        asset_id = assets[0].id
        save_generated_asset_to_gallery(db, image_session_asset_id=asset_id, owner_id=alice_id)

        # B 看画廊：看不到 A 的条目
        bob_entries = list_gallery_entries(db, owner_id=bob_id)
        assert all(entry.image_session_asset_id != asset_id for entry in bob_entries), "B 的画廊里出现了 A 的成品"
        # A 自己看得到
        alice_entries = list_gallery_entries(db, owner_id=alice_id)
        assert any(entry.image_session_asset_id == asset_id for entry in alice_entries)

        # B 无权把 A 的资产投进自己的画廊（归属判定先于"已存在"短路，不泄漏存在性）
        import pytest as _pytest

        from productflow_backend.domain.errors import NotFoundError

        with _pytest.raises(NotFoundError):
            save_generated_asset_to_gallery(db, image_session_asset_id=asset_id, owner_id=bob_id)
    finally:
        db.close()
    _ = alice, bob


def test_isolation_disabled_keeps_current_visibility(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关闭时保持现状：无用户会话也能看到会话列表（整批回退语义）。"""
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("DATA_ISOLATION_ENABLED", "false")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
        alice, _ = _make_user_client(app, db_session, "isoa5")
        created = alice.post("/api/image-sessions", json={"title": "关闭开关的会话"})
        assert created.status_code == 201

        anonymous = TestClient(app)
        listing = anonymous.get("/api/image-sessions").json()
        items = listing.get("items", listing if isinstance(listing, list) else [])
        assert any(item["title"] == "关闭开关的会话" for item in items), "关闭开关时应保持现状可见性"
    finally:
        get_settings.cache_clear()
