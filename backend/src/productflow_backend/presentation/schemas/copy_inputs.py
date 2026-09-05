from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CopyInputExtractionResponse(BaseModel):
    text: str
    source_kind: Literal["text", "image_ocr"]
    warnings: list[str] = Field(default_factory=list)
