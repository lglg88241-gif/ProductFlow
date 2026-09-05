from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


class OcrUnavailableError(RuntimeError):
    """Raised when the local OCR runtime cannot be initialized."""


class OcrProcessingError(RuntimeError):
    """Raised when a validated image cannot be processed by OCR."""


@dataclass(frozen=True, slots=True)
class _OcrFragment:
    text: str
    index: int
    left: float | None = None
    top: float | None = None
    height: float | None = None


def decode_copy_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoded = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        normalized = normalize_copy_text(decoded)
        if not normalized:
            raise ValueError("文案文件内容不能为空")
        return normalized
    raise ValueError("文案文件编码不支持，请使用 UTF-8 或 GB18030")


def normalize_copy_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


@lru_cache(maxsize=1)
def get_ocr_engine() -> Any:
    try:
        from rapidocr import RapidOCR

        return RapidOCR()
    except (ImportError, OSError, RuntimeError) as exc:
        raise OcrUnavailableError("OCR 服务暂不可用") from exc


def extract_text_from_image(content: bytes) -> str:
    try:
        result = get_ocr_engine()(content)
    except OcrUnavailableError:
        raise
    except Exception as exc:
        raise OcrProcessingError("图片文字识别失败") from exc
    return normalize_ocr_result(result)


def normalize_ocr_result(result: object) -> str:
    fragments = _extract_fragments(result)
    if not fragments:
        return ""

    positioned = [fragment for fragment in fragments if fragment.top is not None and fragment.left is not None]
    if len(positioned) != len(fragments):
        return normalize_copy_text("\n".join(fragment.text for fragment in fragments))

    rows: list[list[_OcrFragment]] = []
    for fragment in sorted(positioned, key=lambda item: (item.top or 0, item.left or 0, item.index)):
        center = (fragment.top or 0) + (fragment.height or 1) / 2
        matching_row: list[_OcrFragment] | None = None
        for row in rows:
            row_center = sum((item.top or 0) + (item.height or 1) / 2 for item in row) / len(row)
            row_height = max((item.height or 1) for item in row)
            if abs(center - row_center) <= max(row_height, fragment.height or 1) * 0.6:
                matching_row = row
                break
        if matching_row is None:
            rows.append([fragment])
        else:
            matching_row.append(fragment)

    rows.sort(key=lambda row: min(item.top or 0 for item in row))
    ordered_lines = [
        " ".join(item.text for item in sorted(row, key=lambda fragment: (fragment.left or 0, fragment.index)))
        for row in rows
    ]
    return normalize_copy_text("\n".join(ordered_lines))


def _extract_fragments(result: object) -> list[_OcrFragment]:
    texts = getattr(result, "txts", None)
    if texts is not None:
        boxes = getattr(result, "boxes", None)
        box_items = _as_sequence(boxes)
        return [
            _fragment(text, box_items[index] if index < len(box_items) else None, index)
            for index, text in enumerate(_as_sequence(texts))
            if isinstance(text, str) and text.strip()
        ]

    payload = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (list, tuple)):
        payload = result[0]
    if isinstance(payload, dict):
        payload = payload.get("result") or payload.get("data") or []

    fragments: list[_OcrFragment] = []
    for index, item in enumerate(_as_sequence(payload)):
        box: object | None = None
        text: object | None = None
        if isinstance(item, dict):
            box = item.get("box") or item.get("points")
            text = item.get("text") or item.get("txt")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            box, text = item[0], item[1]
        if isinstance(text, str) and text.strip():
            fragments.append(_fragment(text, box, index))
    return fragments


def _fragment(text: str, box: object | None, index: int) -> _OcrFragment:
    position = _box_position(box)
    if position is None:
        return _OcrFragment(text=text.strip(), index=index)
    left, top, height = position
    return _OcrFragment(text=text.strip(), index=index, left=left, top=top, height=height)


def _box_position(box: object | None) -> tuple[float, float, float] | None:
    values = box.tolist() if hasattr(box, "tolist") else box
    if not isinstance(values, (list, tuple)) or not values:
        return None
    points: list[tuple[float, float]] = []
    for point in values:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(max(ys) - min(ys), 1.0)


def _as_sequence(value: object) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    return list(value) if isinstance(value, (list, tuple)) else []
