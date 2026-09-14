"""朋友圈多图导出：把一张成品切成九宫格/四宫格切片（P2-9）。"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from PIL import Image

from productflow_backend.domain.errors import BusinessError

# 微信朋友圈常用排布：3×3 九宫格、2×2 四宫格、1×3 长图三连
SUPPORTED_GRIDS: dict[str, tuple[int, int]] = {
    "3x3": (3, 3),
    "2x2": (2, 2),
    "3x1": (3, 1),
    "1x3": (1, 3),
}
MAX_TILE_EDGE = 4096


@dataclass(frozen=True, slots=True)
class GridSliceResult:
    grid: str
    rows: int
    columns: int
    tile_size: tuple[int, int]
    archive: bytes

    @property
    def tile_count(self) -> int:
        return self.rows * self.columns


def slice_into_grid(image_bytes: bytes, *, grid: str, fmt: str = "png") -> GridSliceResult:
    """把图片切成分格切片并打包为 zip（按微信发布顺序命名 01..NN）。"""
    if grid not in SUPPORTED_GRIDS:
        raise BusinessError(f"不支持的分格：{grid}；可选 {'、'.join(SUPPORTED_GRIDS)}")
    columns, rows = SUPPORTED_GRIDS[grid]
    output_format = fmt.lower()
    if output_format not in {"png", "jpeg"}:
        raise BusinessError("导出格式只支持 png 或 jpeg")

    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            image = opened.convert("RGB")
    except OSError as exc:
        raise BusinessError("图片无法解码，不能分格导出") from exc

    if max(image.size) > MAX_TILE_EDGE:
        raise BusinessError(f"图片过大（单边超过 {MAX_TILE_EDGE}），请先缩小再导出")

    tile_width = image.width // columns
    tile_height = image.height // rows
    if tile_width <= 0 or tile_height <= 0:
        raise BusinessError("图片尺寸太小，无法切分该分格")

    archive_buffer = io.BytesIO()
    index = 1
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in range(rows):
            for column in range(columns):
                left = column * tile_width
                top = row * tile_height
                # 最后一行/列吃掉整除余数，避免边缘像素丢失
                right = image.width if column == columns - 1 else left + tile_width
                bottom = image.height if row == rows - 1 else top + tile_height
                tile = image.crop((left, top, right, bottom))
                tile_buffer = io.BytesIO()
                if output_format == "jpeg":
                    tile.save(tile_buffer, format="JPEG", quality=92, optimize=True)
                    suffix = "jpg"
                else:
                    tile.save(tile_buffer, format="PNG", optimize=True)
                    suffix = "png"
                archive.writestr(f"{index:02d}.{suffix}", tile_buffer.getvalue())
                index += 1

    return GridSliceResult(
        grid=grid,
        rows=rows,
        columns=columns,
        tile_size=(tile_width, tile_height),
        archive=archive_buffer.getvalue(),
    )
