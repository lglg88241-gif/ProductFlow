"""P3 覆盖率补测：poster/renderer.py 两种海报渲染路径与 queue.py 投递函数。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from productflow_backend.application.contracts import PosterGenerationInput
from productflow_backend.domain.enums import PosterKind
from productflow_backend.infrastructure.poster.renderer import (
    PosterRenderer,
    _draw_wrapped_text,
    _fit_source_image,
    _load_font,
)


def _product_image(tmp_path: Path) -> Path:
    path = tmp_path / "product.png"
    Image.new("RGB", (640, 480), (120, 160, 200)).save(path, format="PNG")
    return path


def _payload(tmp_path: Path, **overrides) -> PosterGenerationInput:
    defaults = dict(
        product_name="轻盈保湿护手霜",
        price="39.90",
        instruction="限时特惠，立即抢购",
        structured_copy_context="摘要：轻盈保湿护手霜\n深层补水 24 小时\n草本温和不刺激\n适合秋冬干燥",
        source_image=_product_image(tmp_path),
    )
    defaults.update(overrides)
    return PosterGenerationInput(**defaults)


def test_render_main_image_poster(configured_env: Path, tmp_path: Path) -> None:
    renderer = PosterRenderer(font_path=tmp_path / "not-exist-font.ttf")
    data = renderer.render(_payload(tmp_path), PosterKind.MAIN_IMAGE)
    with Image.open(BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (1080, 1080)
        # 顶栏为深色：采样顶部像素验证版式元素存在
        top_pixel = image.convert("RGB").getpixel((50, 50))
        assert top_pixel[0] < 60


def test_render_promo_poster(configured_env: Path, tmp_path: Path) -> None:
    renderer = PosterRenderer(font_path=tmp_path / "not-exist-font.ttf")
    data = renderer.render(_payload(tmp_path), PosterKind.PROMO_POSTER)
    with Image.open(BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (1080, 1440)


def test_render_without_source_image_and_sparse_context(configured_env: Path, tmp_path: Path) -> None:
    """无源图、无结构化文案时回退到商品名标题，不崩溃。"""
    renderer = PosterRenderer(font_path=tmp_path / "not-exist-font.ttf")
    payload = PosterGenerationInput(product_name="极简商品", source_image=None)
    for kind in (PosterKind.MAIN_IMAGE, PosterKind.PROMO_POSTER):
        data = renderer.render(payload, kind)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_load_font_falls_back_to_default(tmp_path: Path) -> None:
    font = _load_font(tmp_path / "missing.ttf", 40)
    assert font is not None


def test_fit_source_image_letterboxes(tmp_path: Path) -> None:
    wide = tmp_path / "wide.png"
    Image.new("RGB", (1000, 300), (10, 200, 30)).save(wide, format="PNG")
    fitted = _fit_source_image(wide, (400, 400))
    assert fitted.size == (400, 400)
    # 宽图缩放后上下留白（透明），中心行应包含源图内容
    center = fitted.getpixel((200, 200))
    assert center[1] > 100


def test_draw_wrapped_text_respects_box_height() -> None:
    canvas = Image.new("RGB", (200, 40), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    # 长文本超出盒高时截断而非越界绘制：底部以下区域保持原底色
    _draw_wrapped_text(draw, "超长文字" * 40, font, (0, 0, 200, 30), (0, 0, 0))
    assert canvas.getpixel((150, 35)) == (255, 255, 255)
