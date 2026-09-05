from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from productflow_backend.config import get_runtime_settings
from productflow_backend.infrastructure.copy_inputs import (
    OcrProcessingError,
    OcrUnavailableError,
    decode_copy_text,
    extract_text_from_image,
)
from productflow_backend.presentation.deps import require_admin
from productflow_backend.presentation.schemas.copy_inputs import CopyInputExtractionResponse
from productflow_backend.presentation.upload_validation import read_validated_image_upload

router = APIRouter(prefix="/api/copy-inputs", tags=["copy-inputs"], dependencies=[Depends(require_admin)])

TEXT_INPUT_MAX_BYTES = 2 * 1024 * 1024
TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})


@router.post("/extract", response_model=CopyInputExtractionResponse)
async def extract_copy_input_endpoint(file: UploadFile = File(...)) -> CopyInputExtractionResponse:
    filename = file.filename or "copy-input"
    suffix = Path(filename).suffix.lower()
    declared_mime = (file.content_type or "application/octet-stream").split(";", maxsplit=1)[0].strip().lower()
    is_text_input = declared_mime in TEXT_MIME_TYPES or (
        suffix in TEXT_EXTENSIONS and declared_mime == "application/octet-stream"
    )

    if is_text_input:
        content = await file.read(TEXT_INPUT_MAX_BYTES + 1)
        if len(content) > TEXT_INPUT_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"文案文件超过大小限制: {TEXT_INPUT_MAX_BYTES} bytes",
            )
        if not content:
            raise HTTPException(status_code=400, detail="文案文件内容不能为空")
        try:
            text = decode_copy_text(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CopyInputExtractionResponse(text=text, source_kind="text")

    if declared_mime not in get_runtime_settings().allowed_image_mime_types:
        raise HTTPException(status_code=415, detail=f"不支持的文案文件类型: {declared_mime}")

    validated = await read_validated_image_upload(file, fallback_filename="copy-screenshot.bin")
    try:
        text = extract_text_from_image(validated.content)
    except OcrUnavailableError as exc:
        raise HTTPException(status_code=503, detail="OCR 服务暂不可用，请稍后重试") from exc
    except OcrProcessingError as exc:
        raise HTTPException(status_code=400, detail="图片文字识别失败，请换一张清晰图片") from exc
    if not text:
        raise HTTPException(status_code=400, detail="未识别到可用文字")
    return CopyInputExtractionResponse(text=text, source_kind="image_ocr")
