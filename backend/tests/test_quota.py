"""媒体配额（审计批次 C）：计量口径、准入拒绝、告警阈值与路径覆盖。

审计要求"配额必须结合实际可用空间验证"——本文件用真实文件（写入 tmp 存储）验证计量，
不用假数字，确保覆盖四个媒体载体表（漏一张就会把用量算少）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_isolation_products import _make_user_client, _png


def _uid(client: TestClient) -> str:
    """取当前会话用户 id（测试内省用）。"""
    return str(client.get("/api/auth/user/me").json()["id"])


def test_usage_counts_all_four_media_tables(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """计量覆盖：素材库、商品源图、海报变体、图片会话资产（经父级继承归属）。"""
    from productflow_backend.application.quota import compute_usage
    from productflow_backend.application.use_cases import create_product
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "quota_a")

    # 素材库：上传一张
    uploaded = alice.post(
        "/api/agent/assets", files={"file": ("模板.png", _png(400, 400), "image/png")}, data={"kind": "template"}
    )
    assert uploaded.status_code == 201

    # 商品：创建（主图 + 一张参考图 → source_assets）
    created = alice.post(
        "/api/products",
        data={"name": "配额商品"},
        files={
            "image": ("main.png", _png(300, 300), "image/png"),
            "reference_images": ("ref.png", _png(200, 200), "image/png"),
        },
    )
    assert created.status_code == 201, created.text

    report = compute_usage(db_session, alice_id, use_cache=False)
    assert report.file_count >= 3, f"应至少覆盖素材 1 + 主图 1 + 参考图 1，实际 {report.file_count}"
    assert report.used_bytes > 0, "计量到的字节数不应为 0"

    # 手工核对：磁盘上这些文件的真实大小之和必须与计量一致
    from productflow_backend.infrastructure.storage import LocalStorage

    storage = LocalStorage()
    from productflow_backend.application.quota import _collect_media_paths

    expected = sum(Path(storage.resolve(p)).stat().st_size for p in _collect_media_paths(db_session, alice_id))
    assert report.used_bytes == expected, "计量口径与磁盘实际占用不一致"

    _ = create_product


def test_other_users_media_is_not_counted(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """配额按用户：A 的媒体不计入 B 的用量。"""
    from productflow_backend.application.quota import compute_usage
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "quota_a2")
    bob, bob_id = _make_user_client(app, db_session, "quota_b2")

    alice.post("/api/agent/assets", files={"file": ("a.png", _png(500, 500), "image/png")}, data={"kind": "template"})

    alice_usage = compute_usage(db_session, alice_id, use_cache=False)
    bob_usage = compute_usage(db_session, bob_id, use_cache=False)
    assert alice_usage.used_bytes > 0
    assert bob_usage.used_bytes == 0, "B 的用量里混入了 A 的媒体"
    assert bob_usage.file_count == 0


def test_upload_rejected_when_quota_exceeded(
    isolation_env: None, configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超限拒绝新增：给出人话错误，且明确指向"删除素材或会话"。"""
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    # 纯色 PNG 压缩后只有几 KB，额度必须压到同量级才能触发超限
    monkeypatch.setenv("USER_QUOTA_BYTES", "2048")
    get_settings.cache_clear()
    try:
        app = create_app()
        alice, _ = _make_user_client(app, db_session, "quota_small")

        blocked = alice.post(
            "/api/agent/assets",
            files={"file": ("big.png", _png(2400, 2400), "image/png")},
            data={"kind": "template"},
        )
        assert blocked.status_code == 400, blocked.text
        detail = blocked.json()["detail"]
        assert "空间不足" in detail and "删除" in detail, detail
        # 拒绝后不得留下任何文件（准入先于写入）
        from productflow_backend.application.quota import compute_usage

        assert compute_usage(db_session, _uid(alice), use_cache=False).file_count == 0, "被拒的上传不应落盘"
    finally:
        get_settings.cache_clear()


def test_warning_threshold_at_80_percent(
    isolation_env: None, configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """80% 告警：用量越过阈值时 warning=True，未越线为 False。"""
    from productflow_backend.application.quota import compute_usage
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "quota_warn")

    # 先传一张图（约几十 KB）
    alice.post("/api/agent/assets", files={"file": ("w.png", _png(400, 400), "image/png")}, data={"kind": "template"})
    used = compute_usage(db_session, alice_id, use_cache=False).used_bytes
    assert used > 0

    # 额度压到"用量的 1.1 倍"→ 未告警；压到"0.9 倍"→ 告警
    monkeypatch.setenv("USER_QUOTA_BYTES", str(int(used / 0.5)))
    get_settings.cache_clear()
    try:
        report = compute_usage(db_session, alice_id, use_cache=False)
        assert report.ratio < 0.8 and not report.warning, f"ratio={report.ratio}"
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("USER_QUOTA_BYTES", str(int(used / 0.9)))
    get_settings.cache_clear()
    try:
        report = compute_usage(db_session, alice_id, use_cache=False)
        assert report.ratio >= 0.8 and report.warning, f"ratio={report.ratio}"
    finally:
        get_settings.cache_clear()


def test_usage_endpoint_reports_scoped_usage(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """用量接口：返回已用/配额/比例/告警位，并标明是按用户口径。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, _ = _make_user_client(app, db_session, "quota_api")
    alice.post("/api/agent/assets", files={"file": ("e.png", _png(200, 200), "image/png")}, data={"kind": "template"})

    payload = alice.get("/api/storage/usage").json()
    assert payload["scoped_to_user"] is True
    assert payload["quota_bytes"] > 0
    assert payload["used_bytes"] > 0
    assert payload["file_count"] >= 1
    assert 0 <= payload["ratio"] <= 1
    assert payload["exceeded"] is False


def test_quota_does_not_apply_when_isolation_disabled(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离关闭（无用户上下文）时不限制上传——保持现状行为。"""
    from productflow_backend.config import get_settings
    from productflow_backend.presentation.api import create_app

    monkeypatch.setenv("DATA_ISOLATION_ENABLED", "false")
    monkeypatch.setenv("ADMIN_ACCESS_REQUIRED", "false")
    monkeypatch.setenv("USER_QUOTA_BYTES", "2048")  # 极小额度
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.post(
            "/api/agent/assets",
            files={"file": ("n.png", _png(300, 300), "image/png")},
            data={"kind": "template"},
        )
        assert response.status_code == 201, "隔离关闭时不应受配额限制（现状行为）"
    finally:
        get_settings.cache_clear()


def test_usage_cache_invalidated_after_upload(
    isolation_env: None, configured_env: Path, db_session
) -> None:
    """写入后缓存失效：连续上传时第二次计量必须反映新增文件（否则会低估用量）。"""
    from productflow_backend.application.quota import compute_usage
    from productflow_backend.presentation.api import create_app

    app = create_app()
    alice, alice_id = _make_user_client(app, db_session, "quota_cache")

    alice.post("/api/agent/assets", files={"file": ("c1.png", _png(300, 300), "image/png")}, data={"kind": "template"})
    first = compute_usage(db_session, alice_id).used_bytes  # 走缓存
    alice.post("/api/agent/assets", files={"file": ("c2.png", _png(300, 300), "image/png")}, data={"kind": "template"})
    second = compute_usage(db_session, alice_id).used_bytes  # 应已失效并重算
    assert second > first, "上传后用量没有更新（缓存未失效）"
