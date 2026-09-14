"""参数槽重渲工具 rerender_poster_copy（功能 B）：文字覆盖 → 本地重渲 → 新变体落库可下载。"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _login
from PIL import Image

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall


def _png_bytes(width: int = 900, height: int = 900) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (180, 90, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


def _create_product_with_image(db, *, price: str | None = "59.90"):
    from productflow_backend.application.use_cases import create_product

    return create_product(
        db,
        name="山茶花护手霜",
        category="个护",
        price=price,
        source_note=None,
        image_bytes=_png_bytes(),
        filename="product.png",
        content_type="image/png",
    )


def _run_rerender_turn(install_scripted_llm, arguments: dict):
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[AgentToolCall(call_id="call-rerender-1", name="rerender_poster_copy", arguments=arguments)],
            ),
            AgentLLMResponse(content="文字改好了，新海报已经出图。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="改价格")
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="把价格改成 49.9，主标题换一下")
        tool_result = json.loads([m for m in result.messages if m.role == "tool"][-1].content)
        return tool_result
    finally:
        db.close()


def test_rerender_poster_copy_creates_new_variant_and_download(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.infrastructure.db.models import PosterVariant
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        product = _create_product_with_image(db)
        product_id = product.id
        variant_count_before = db.query(PosterVariant).filter_by(product_id=product_id).count()
    finally:
        db.close()

    tool_result = _run_rerender_turn(
        install_scripted_llm,
        {
            "product_id": product_id,
            "poster_kind": "main_image",
            "copy": {"price": "49.9", "headline": "山茶花新品首发"},
        },
    )
    assert tool_result["status"] == "ok", tool_result
    assert tool_result["poster_kind"] == "main_image"
    assert sorted(tool_result["changed_fields"]) == ["headline", "price"]
    assert tool_result["download_url"] == f"/api/posters/{tool_result['poster_id']}/download"

    db = get_session_factory()()
    try:
        variants = db.query(PosterVariant).filter_by(product_id=product_id).all()
        assert len(variants) == variant_count_before + 1
        variant = next(item for item in variants if item.id == tool_result["poster_id"])
        assert variant.kind == "main_image"
        assert variant.mime_type == "image/png"
        assert (variant.width, variant.height) == (1080, 1080)
        assert variant.copy_set_id
    finally:
        db.close()

    # 下载链接可访问，内容是一张真实 PNG
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    _login(client)
    downloaded = client.get(tool_result["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "image/png"
    with Image.open(BytesIO(downloaded.content)) as image:
        assert image.size == (1080, 1080)


def test_rerender_poster_copy_promo_poster_reuses_latest_copy_set(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.infrastructure.db.models import PosterVariant
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        product = _create_product_with_image(db)
        product_id = product.id
    finally:
        db.close()

    tool_result = _run_rerender_turn(
        install_scripted_llm,
        {
            "product_id": product_id,
            "poster_kind": "promo_poster",
            "copy": {"selling_points": ["48 小时长效保湿", "山茶花精粹", "不油腻"], "instruction": "开业特惠 立即抢购"},
        },
    )
    assert tool_result["status"] == "ok", tool_result
    assert tool_result["poster_kind"] == "promo_poster"
    assert sorted(tool_result["changed_fields"]) == ["instruction", "selling_points"]

    db = get_session_factory()()
    try:
        variant = db.get(PosterVariant, tool_result["poster_id"])
        assert variant is not None
        assert (variant.width, variant.height) == (1080, 1440)
        assert variant.copy_set_id  # 无文案集时自动落占位文案集承接变体
    finally:
        db.close()


def test_rerender_poster_copy_reports_friendly_errors(configured_env: Path, install_scripted_llm) -> None:
    tool_result = _run_rerender_turn(
        install_scripted_llm,
        {"product_id": "no-such-product", "poster_kind": "main_image", "copy": {"price": "1"}},
    )
    assert tool_result["status"] == "error"
    assert "找不到这个商品" in tool_result["message"]


def test_rerender_poster_copy_rejects_empty_copy(configured_env: Path, install_scripted_llm) -> None:
    tool_result = _run_rerender_turn(
        install_scripted_llm,
        {"product_id": "whatever", "poster_kind": "main_image", "copy": {"unknown_field": "x"}},
    )
    assert tool_result["status"] == "error"
    assert "没有可识别的文字字段" in tool_result["message"]
