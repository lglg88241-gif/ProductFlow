"""端到端用户旅程冒烟测试：模拟运营者从登录到交付物清理的完整使用路径。

与单元测试不同，这里始终通过 HTTP API 走完整链路（含应用 lifespan 启动、
供应商引导与队列恢复），验证模块协作后的对外行为与落盘产物，而非内部函数。
生成走 mock 供应商，队列以 inline 方式同步执行以保持确定性。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    _execute_workflow_queue_inline,
    _login,
    _unlock_settings,
    _wait_for_workflow_run,
)
from PIL import Image


def _png_bytes(width: int = 800, height: int = 800) -> bytes:
    image = Image.new("RGB", (width, height), (238, 242, 246))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _assert_image_response(content: bytes, *, max_edge: int | None = None) -> tuple[int, int]:
    with Image.open(BytesIO(content)) as image:
        size = image.size
    if max_edge is not None:
        assert max(size) <= max_edge
    return size


@pytest.fixture(autouse=True)
def _inline_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    _execute_workflow_queue_inline(monkeypatch)


def test_full_operator_journey_from_login_to_deliverable_cleanup(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    with TestClient(create_app()) as client:
        # 1. 启动即健康，受保护 API 未登录不可见
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "admin_access_required": True}
        assert client.get("/api/products").status_code == 401

        # 2. 错误密钥拒绝，正确密钥登录
        assert client.post("/api/auth/session", json={"admin_key": "not-the-key"}).status_code == 401
        _login(client)

        # 3. 建品：主图上传 + 画布模板物化节点（default 为惰性建图，这里用完整模板）
        created = client.post(
            "/api/products",
            data={
                "name": "旅程测试护手霜",
                "category": "个护",
                "price": "39.90",
                "canvas_template_key": "ecommerce-main-image-v1",
            },
            files={"image": ("product.png", _png_bytes(), "image/png")},
        )
        assert created.status_code == 201, created.text
        product_id = created.json()["id"]
        product_root = configured_env / "products" / product_id
        assert product_root.exists()
        workflow = client.get(f"/api/products/{product_id}/workflow")
        assert workflow.status_code == 200
        assert workflow.json()["nodes"], "画布模板应物化出工作流节点"

        # 4. 上传参考图
        reference = client.post(
            f"/api/products/{product_id}/reference-images",
            files=[("reference_images", ("ref.png", _png_bytes(640, 640), "image/png"))],
        )
        assert reference.status_code == 200
        assert any(asset["kind"] == "reference_image" for asset in reference.json()["source_assets"])

        # 5. 一键跑工作流：mock 供应商产出文案与海报
        run = client.post(f"/api/products/{product_id}/workflow/run", json={})
        assert run.status_code == 200
        payload = _wait_for_workflow_run(client, product_id, status="succeeded")
        assert payload["runs"][0]["status"] == "succeeded"
        assert payload["nodes"], "运行应包含节点结果"
        assert all(node["status"] == "succeeded" for node in payload["nodes"])

        # 6. 交付物可下载且是真实图片
        detail = client.get(f"/api/products/{product_id}")
        assert detail.status_code == 200
        posters = detail.json()["poster_variants"]
        assert posters, "工作流成功后应有海报产出"
        poster_download = client.get(posters[0]["download_url"])
        assert poster_download.status_code == 200
        _assert_image_response(poster_download.content)
        poster_thumbnail = client.get(posters[0]["thumbnail_url"])
        assert poster_thumbnail.status_code == 200
        _assert_image_response(poster_thumbnail.content, max_edge=320)

        # 7. 文/图生图会话：生成、下载缩略图、存入画廊
        session_created = client.post("/api/image-sessions", json={"title": "旅程会话"})
        assert session_created.status_code == 201
        session_id = session_created.json()["id"]
        generated = client.post(
            f"/api/image-sessions/{session_id}/generate",
            json={"prompt": "奶油质感护手霜广告图，柔光白底", "size": "1024x1024"},
        )
        assert generated.status_code == 202
        rounds = generated.json()["rounds"]
        assert rounds, "inline 队列应同步产出候选图"
        asset = rounds[-1]["generated_asset"]
        session_thumbnail = client.get(asset["thumbnail_url"])
        assert session_thumbnail.status_code == 200
        _assert_image_response(session_thumbnail.content, max_edge=320)

        saved_to_gallery = client.post("/api/gallery", json={"image_session_asset_id": asset["id"]})
        assert saved_to_gallery.status_code == 201
        gallery = client.get("/api/gallery")
        assert gallery.status_code == 200
        assert any(item["image"]["id"] == asset["id"] for item in gallery.json()["items"])

        # 8. 生成队列总览可见
        queue = client.get("/api/generation-queue")
        assert queue.status_code == 200

        # 9. 运行时配置修改即时生效（解锁 → 修改 → 运行时读取）
        _unlock_settings(client)
        patched = client.patch("/api/settings", json={"values": {"deletion_enabled": True}})
        assert patched.status_code == 200
        runtime = client.get("/api/settings/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["deletion_enabled"] is True

        # 10. 删除商品：业务数据与落盘文件一并清理
        deleted = client.delete(f"/api/products/{product_id}")
        assert deleted.status_code == 204
        assert not product_root.exists(), "删除商品应清空其存储目录"
        assert client.get(f"/api/products/{product_id}").status_code == 404

        # 11. 登出后受保护 API 重新不可见
        assert client.delete("/api/auth/session").status_code == 200
        assert client.get("/api/products").status_code == 401
