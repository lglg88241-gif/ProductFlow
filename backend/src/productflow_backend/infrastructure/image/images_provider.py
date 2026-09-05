"""OpenAI Images API provider (/v1/images/generations, /v1/images/edits).

Supports any OpenAI-compatible image generation endpoint (DALL-E, SD WebUI, ComfyUI wrappers, etc.).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from openai import OpenAI

from productflow_backend.application.contracts import PosterGenerationInput
from productflow_backend.application.language_policy import image_visible_text_requirements
from productflow_backend.application.poster_prompt_context import build_poster_context_block
from productflow_backend.config import get_runtime_settings
from productflow_backend.domain.enums import PosterKind
from productflow_backend.infrastructure.image.base import (
    GeneratedImagePayload,
    ImageProvider,
    decode_b64_image,
    image_dimensions_from_bytes,
    parse_size,
)
from productflow_backend.infrastructure.image.responses_provider import (
    build_responses_reference_images_from_poster,
    poster_has_reference_input,
)
from productflow_backend.infrastructure.prompts import render_prompt_template
from productflow_backend.infrastructure.provider_config import (
    ResolvedImageProviderConfig,
    resolve_image_provider_config,
)

logger = logging.getLogger(__name__)

PROVIDER_REQUEST_FAILURE_MESSAGE = "图片供应商请求失败，请检查供应商配置后重试"
PROVIDER_MISSING_OUTPUT_MESSAGE = "图片供应商没有返回图片结果，请稍后重试"
OPTIONAL_FIELDS_FALLBACK_NOTE = {
    "kind": "fallback",
    "message": "供应商不支持部分可选参数，已按基础参数完成。",
}
IMAGES_API_MAX_N = 10
IMAGES_API_TRANSIENT_RETRIES = 2
_OPTIONAL_IMAGE_FIELDS = frozenset(
    {
        "quality",
        "style",
        "output_format",
        "output_compression",
        "background",
        "moderation",
    }
)
_OPTIONAL_FIELD_ERROR_MARKERS = (
    "unsupported",
    "not supported",
    "not_supported",
    "unknown parameter",
    "unknown field",
    "unrecognized",
    "unexpected keyword",
    "unexpected argument",
    "invalid parameter",
    "invalid field",
    "unsupported optional",
    "额外参数",
    "参数不支持",
)
_IMAGE_INPUT_ERROR_MARKERS = (
    "multiple file",
    "multiple image",
    "too many image",
    "image input",
    "invalid image",
    "unsupported image",
    "corrupt image",
    "image format",
    "图片输入",
    "参考图",
    "多张图片",
)


@dataclass(slots=True)
class ImagesAPIResult:
    bytes_data: bytes
    mime_type: str
    model_name: str
    size: str
    generated_at: datetime
    revised_prompt: str | None
    provider_request_id: str | None
    provider_request_json: dict[str, Any]
    provider_output_json: dict[str, Any]


@dataclass(slots=True)
class ImagesReferenceImage:
    bytes_data: bytes
    mime_type: str
    filename: str


def _mime_type_from_image_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class OpenAIImagesClient:
    """Thin wrapper around the OpenAI Images API (generations + edits)."""

    provider_name = "openai-images"

    def __init__(self, provider_config: ResolvedImageProviderConfig | None = None) -> None:
        resolved_config = provider_config or resolve_image_provider_config()
        self.api_key = resolved_config.api_key
        self.base_url = resolved_config.base_url
        self.model = resolved_config.model
        self.quality = resolved_config.images_quality
        self.style = resolved_config.images_style

    def _client(self) -> OpenAI:
        if not self.api_key:
            raise RuntimeError("图片供应商档案缺少 API Key")
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _parse_response(
        self,
        response: Any,
        *,
        model: str,
        size: str,
        provider_request_json: dict[str, Any],
        provider_output_json: dict[str, Any] | None = None,
    ) -> list[ImagesAPIResult]:
        results: list[ImagesAPIResult] = []
        now = datetime.now(UTC)
        provider_request_id = self._request_id_from_value(response)
        safe_provider_output = dict(provider_output_json or {})
        if provider_request_id:
            productflow_metadata = dict(safe_provider_output.get("_productflow") or {})
            productflow_metadata["provider_request_id"] = provider_request_id
            safe_provider_output["_productflow"] = productflow_metadata
        for item in getattr(response, "data", []) or []:
            b64 = getattr(item, "b64_json", None)
            if not b64:
                continue
            image_bytes = decode_b64_image(b64)
            results.append(
                ImagesAPIResult(
                    bytes_data=image_bytes,
                    mime_type=_mime_type_from_image_bytes(image_bytes),
                    model_name=model,
                    size=size,
                    generated_at=now,
                    revised_prompt=getattr(item, "revised_prompt", None),
                    provider_request_id=provider_request_id,
                    provider_request_json=provider_request_json,
                    provider_output_json=safe_provider_output,
                )
            )

        if not results:
            raise RuntimeError(PROVIDER_MISSING_OUTPUT_MESSAGE)
        return results

    @staticmethod
    def _request_id_from_value(value: Any) -> str | None:
        for attribute in ("_request_id", "request_id", "requestID"):
            request_id = getattr(value, attribute, None)
            if request_id:
                return str(request_id)[:255]
        response = getattr(value, "response", None) or getattr(value, "http_response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            request_id = headers.get("x-request-id") or headers.get("request-id")
            if request_id:
                return str(request_id)[:255]
        return None

    def _log_provider_error(self, operation: str, exc: BaseException, *, model: str) -> None:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        code = getattr(exc, "code", None)
        request_id = self._request_id_from_value(exc)
        logger.error(
            "OpenAI Images API %s failed: model=%s status=%s code=%s request_id=%s error_class=%s",
            operation,
            model,
            status_code,
            code,
            request_id,
            type(exc).__name__,
        )

    def _with_productflow_metadata(
        self,
        provider_output_json: dict[str, Any] | None,
        *,
        notes: list[dict[str, Any]],
        requested_image_count: int | None = None,
        effective_image_count: int | None = None,
    ) -> dict[str, Any]:
        output = dict(provider_output_json or {})
        metadata = dict(output.get("_productflow") or {})
        if notes:
            metadata["notes"] = notes
        if requested_image_count is not None:
            metadata["requested_image_count"] = requested_image_count
        if effective_image_count is not None:
            metadata["effective_image_count"] = effective_image_count
        if metadata:
            output["_productflow"] = metadata
        return output

    def _should_retry_without_optional_fields(self, request_params: dict[str, Any]) -> bool:
        return any(key in request_params for key in _OPTIONAL_IMAGE_FIELDS)

    @staticmethod
    def _exception_diagnostic_text(exc: BaseException) -> str:
        """Collect provider error fields for classification without exposing them to callers."""
        parts: list[str] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            parts.append(str(current))
            for attribute in ("code", "param", "message", "body"):
                value = getattr(current, attribute, None)
                if value is not None:
                    parts.append(str(value))
            response = getattr(current, "response", None)
            if response is not None:
                response_body = getattr(response, "text", None) or getattr(response, "content", None)
                if response_body:
                    parts.append(str(response_body))
            current = current.__cause__ or current.__context__
        return " ".join(parts).lower()

    def _is_optional_parameter_error(
        self,
        exc: BaseException,
        request_params: dict[str, Any],
    ) -> bool:
        """Return true only for an error that names unsupported optional fields.

        A generic 400 or a multi-image/input failure must not trigger a fallback:
        dropping a reference image changes the user's requested edit.
        """
        if not self._should_retry_without_optional_fields(request_params) or self._is_transient_error(exc):
            return False
        diagnostic = self._exception_diagnostic_text(exc)
        if any(marker in diagnostic for marker in _IMAGE_INPUT_ERROR_MARKERS):
            return False
        optional_field_named = (
            any(field in diagnostic for field in _OPTIONAL_IMAGE_FIELDS)
            or "optional field" in diagnostic
            or "可选参数" in diagnostic
        )
        return optional_field_named and any(marker in diagnostic for marker in _OPTIONAL_FIELD_ERROR_MARKERS)

    def _safe_request_failure_message(self, exc: BaseException, *, multiple_images: bool = False) -> str:
        """Map common provider failures to a useful next step without raw provider text."""
        diagnostic = self._exception_diagnostic_text(exc)
        if any(
            marker in diagnostic
            for marker in ("moderation_blocked", "content policy", "safety policy", "policy violation")
        ):
            return "图片生成被内容安全策略拦截，请调整提示词或参考图后重试"
        if "moderation" in diagnostic and any(marker in diagnostic for marker in ("block", "reject", "refus", "den")):
            return "图片生成被内容安全策略拦截，请调整提示词或参考图后重试"
        if "organization" in diagnostic and any(marker in diagnostic for marker in ("verif", "gpt image", "access")):
            return "当前 OpenAI 组织尚未完成 GPT Image 验证，请在组织设置中完成验证后重试"
        if multiple_images and any(marker in diagnostic for marker in _IMAGE_INPUT_ERROR_MARKERS):
            return "图片供应商无法处理当前多图编辑输入，请检查参考图格式和数量，或更换支持多图编辑的 provider"
        if any(
            marker in diagnostic
            for marker in ("invalid image", "unsupported image", "corrupt image", "image format", "图片输入")
        ):
            return "图片输入无法读取，请检查当前作品和参考图的格式、尺寸后重试"
        return PROVIDER_REQUEST_FAILURE_MESSAGE

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        try:
            normalized_status = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            normalized_status = None
        if normalized_status == 429 or (normalized_status is not None and 500 <= normalized_status <= 599):
            return True
        return bool(
            re.search(
                r"\b429\b|\b5\d\d\b|temporarily unavailable|server error",
                str(exc),
                re.IGNORECASE,
            )
        )

    def _call_with_transient_retries(self, operation: Any, request_params: dict[str, Any]) -> Any:
        """Retry only upstream throttling/server failures, never input errors."""
        for attempt in range(IMAGES_API_TRANSIENT_RETRIES + 1):
            self._rewind_request_files(request_params)
            try:
                return operation(**request_params)
            except Exception as exc:  # noqa: BLE001
                if not self._is_transient_error(exc) or attempt >= IMAGES_API_TRANSIENT_RETRIES:
                    raise
                logger.warning(
                    "OpenAI Images API transient failure; retrying: attempt=%s error_class=%s",
                    attempt + 1,
                    type(exc).__name__,
                )
        raise AssertionError("unreachable transient retry state")

    @staticmethod
    def _rewind_request_files(request_params: dict[str, Any]) -> None:
        """The OpenAI SDK consumes file handles; every retry must start at byte zero."""
        for key in ("image", "mask"):
            value = request_params.get(key)
            values = value if isinstance(value, (list, tuple)) else (value,)
            for file in values:
                seek = getattr(file, "seek", None)
                if callable(seek):
                    try:
                        seek(0)
                    except (OSError, ValueError):
                        continue

    def generate(
        self,
        *,
        prompt: str,
        size: str,
        model: str | None = None,
        quality: str | None = None,
        style: str | None = None,
        n: int = 1,
        output_format: str | None = None,
        output_compression: int | None = None,
        background: str | None = None,
        moderation: str | None = None,
    ) -> list[ImagesAPIResult]:
        client = self._client()
        req_model = model or self.model
        req_quality = quality or self.quality or "medium"
        req_style = style or self.style

        request_params: dict[str, Any] = {
            "model": req_model,
            "prompt": prompt,
            "size": size,
            "n": n,
        }
        if req_model != "gpt-image-2":
            request_params["response_format"] = "b64_json"
        if req_quality:
            request_params["quality"] = req_quality
        if req_style and req_model != "gpt-image-2":
            request_params["style"] = req_style
        if output_format:
            request_params["output_format"] = output_format
        if output_compression is not None:
            request_params["output_compression"] = output_compression
        if background:
            request_params["background"] = background
        if moderation:
            request_params["moderation"] = moderation

        fallback_used = False
        try:
            response = self._call_with_transient_retries(client.images.generate, request_params)
        except Exception as exc:  # noqa: BLE001
            if not self._is_optional_parameter_error(exc, request_params):
                self._log_provider_error("generate", exc, model=req_model)
                raise RuntimeError(self._safe_request_failure_message(exc)) from exc
            fallback_used = True
            fallback_params = {
                key: value for key, value in request_params.items() if key not in _OPTIONAL_IMAGE_FIELDS
            }
            try:
                response = self._call_with_transient_retries(client.images.generate, fallback_params)
                request_params = fallback_params
            except Exception as fallback_exc:  # noqa: BLE001
                self._log_provider_error("generate_optional_fallback", fallback_exc, model=req_model)
                raise RuntimeError(self._safe_request_failure_message(fallback_exc)) from fallback_exc

        provider_output_json = self._with_productflow_metadata(
            None,
            notes=[OPTIONAL_FIELDS_FALLBACK_NOTE] if fallback_used else [],
        )
        return self._parse_response(
            response,
            model=req_model,
            size=size,
            provider_request_json=self._sanitize_generate_request_params(request_params),
            provider_output_json=provider_output_json,
        )

    def edit(
        self,
        *,
        image: bytes | Sequence[ImagesReferenceImage],
        prompt: str,
        size: str,
        mask: bytes | None = None,
        model: str | None = None,
        quality: str | None = None,
        n: int = 1,
        output_format: str | None = None,
        output_compression: int | None = None,
        background: str | None = None,
        moderation: str | None = None,
    ) -> list[ImagesAPIResult]:
        client = self._client()
        req_model = model or self.model
        req_quality = quality or self.quality or "medium"

        image_files, image_metadata = self._build_image_files(image)

        request_params: dict[str, Any] = {
            "model": req_model,
            "image": image_files[0] if len(image_files) == 1 else image_files,
            "prompt": prompt,
            "size": size,
            "n": n,
        }
        if req_model != "gpt-image-2":
            request_params["response_format"] = "b64_json"
        if req_quality:
            request_params["quality"] = req_quality
        if output_format:
            request_params["output_format"] = output_format
        if output_compression is not None:
            request_params["output_compression"] = output_compression
        if background:
            request_params["background"] = background
        if moderation:
            request_params["moderation"] = moderation
        if mask is not None:
            mask_file = BytesIO(mask)
            mask_file.name = "mask.png"
            request_params["mask"] = mask_file

        log_params = self._sanitize_edit_request_params(
            request_params,
            image_count=len(image_files),
            image_metadata=image_metadata,
            has_mask=mask is not None,
        )

        fallback_notes: list[dict[str, Any]] = []
        requested_image_count = len(image_files)
        effective_image_count = len(image_files)
        try:
            response = self._call_with_transient_retries(client.images.edit, request_params)
        except Exception as exc:  # noqa: BLE001
            if not self._is_optional_parameter_error(exc, request_params):
                self._log_provider_error("edit", exc, model=req_model)
                raise RuntimeError(
                    self._safe_request_failure_message(exc, multiple_images=len(image_files) > 1)
                ) from exc
            fallback_params = {
                key: value for key, value in request_params.items() if key not in _OPTIONAL_IMAGE_FIELDS
            }
            fallback_notes.append(OPTIONAL_FIELDS_FALLBACK_NOTE)
            try:
                response = self._call_with_transient_retries(client.images.edit, fallback_params)
                request_params = fallback_params
                log_params = self._sanitize_edit_request_params(
                    request_params,
                    image_count=effective_image_count,
                    image_metadata=image_metadata,
                    has_mask=mask is not None,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                self._log_provider_error("edit_optional_fallback", fallback_exc, model=req_model)
                raise RuntimeError(
                    self._safe_request_failure_message(fallback_exc, multiple_images=len(image_files) > 1)
                ) from fallback_exc

        provider_output_json = self._with_productflow_metadata(
            None,
            notes=fallback_notes,
            requested_image_count=requested_image_count,
            effective_image_count=effective_image_count,
        )
        return self._parse_response(
            response,
            model=req_model,
            size=size,
            provider_request_json=log_params,
            provider_output_json=provider_output_json,
        )

    def _build_image_files(
        self,
        image: bytes | Sequence[ImagesReferenceImage],
    ) -> tuple[list[BytesIO], list[dict[str, str]]]:
        if isinstance(image, bytes):
            image_file = BytesIO(image)
            image_file.name = "image.png"
            return [image_file], [{"filename": "image.png", "mime_type": _mime_type_from_image_bytes(image)}]

        files: list[BytesIO] = []
        metadata: list[dict[str, str]] = []
        for index, reference in enumerate(image, start=1):
            image_file = BytesIO(reference.bytes_data)
            image_file.name = reference.filename or f"image-{index}.png"
            files.append(image_file)
            metadata.append({"filename": image_file.name, "mime_type": reference.mime_type})
        if not files:
            raise RuntimeError("图片供应商缺少编辑输入图片")
        return files, metadata

    def _sanitize_edit_request_params(
        self,
        request_params: dict[str, Any],
        *,
        image_count: int,
        image_metadata: list[dict[str, str]],
        has_mask: bool,
    ) -> dict[str, Any]:
        log_params = {
            key: value
            for key, value in request_params.items()
            if key not in {"image", "mask", "prompt", "response_format"}
        }
        log_params["prompt_length"] = len(str(request_params.get("prompt") or ""))
        log_params["image_count"] = image_count
        log_params["images"] = image_metadata
        log_params["has_mask"] = has_mask
        return log_params

    @staticmethod
    def _sanitize_generate_request_params(request_params: dict[str, Any]) -> dict[str, Any]:
        log_params = {
            key: value
            for key, value in request_params.items()
            if key not in {"prompt", "response_format"}
        }
        log_params["prompt_length"] = len(str(request_params.get("prompt") or ""))
        return log_params


class OpenAIImagesImageProvider(ImageProvider):
    """ImageProvider implementation backed by the standard OpenAI Images API."""

    provider_name = "openai-images"
    prompt_version = "images-api-v1"

    def __init__(self, provider_config: ResolvedImageProviderConfig | None = None) -> None:
        self.provider_config = provider_config or resolve_image_provider_config()

    def generate_poster_image(
        self,
        poster: PosterGenerationInput,
        kind: PosterKind,
    ) -> tuple[GeneratedImagePayload, str]:
        return self.generate_poster_images(poster=poster, kind=kind, count=1)[0]

    def generate_poster_images(
        self,
        poster: PosterGenerationInput,
        kind: PosterKind,
        count: int,
    ) -> list[tuple[GeneratedImagePayload, str]]:
        if count <= 0:
            return []
        settings = get_runtime_settings()
        client = OpenAIImagesClient(self.provider_config)

        size = poster.image_size or (
            settings.image_main_image_size if kind == PosterKind.MAIN_IMAGE else settings.image_promo_poster_size
        )
        prompt = self._build_prompt(poster, kind, size, settings)
        reference_images = self._build_reference_images_from_poster(poster)
        request_options = self._request_options_from_tool_options(poster.tool_options)
        results: list[ImagesAPIResult] = []
        remaining = count

        while remaining > 0:
            batch_count = min(remaining, IMAGES_API_MAX_N)
            if reference_images:
                batch_results = client.edit(
                    image=reference_images,
                    prompt=prompt,
                    size=size,
                    n=batch_count,
                    **request_options,
                )
            else:
                batch_results = client.generate(prompt=prompt, size=size, n=batch_count, **request_options)
            results.extend(batch_results)
            if len(batch_results) < batch_count:
                break
            remaining -= batch_count

        if len(results) < count:
            raise RuntimeError(PROVIDER_MISSING_OUTPUT_MESSAGE)

        return [
            (self._payload_from_images_result(result, kind=kind, size=size, index=index), result.model_name)
            for index, result in enumerate(results[:count], start=1)
        ]

    def _request_options_from_tool_options(self, tool_options: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(tool_options, dict):
            return {}
        options: dict[str, Any] = {}
        model = self._optional_tool_text(tool_options.get("model"))
        quality = self._optional_tool_text(tool_options.get("quality"))
        output_format = self._optional_tool_text(tool_options.get("output_format"))
        output_compression = tool_options.get("output_compression")
        background = self._optional_tool_text(tool_options.get("background"))
        moderation = self._optional_tool_text(tool_options.get("moderation"))
        if model:
            options["model"] = model
        if quality:
            options["quality"] = quality
        if output_format:
            options["output_format"] = output_format
        if isinstance(output_compression, int) and not isinstance(output_compression, bool):
            options["output_compression"] = output_compression
        if background:
            options["background"] = background
        if moderation:
            options["moderation"] = moderation
        return options

    def _optional_tool_text(self, value: Any) -> str | None:
        normalized = "" if value is None else str(value).strip()
        return normalized or None

    def _payload_from_images_result(
        self,
        result: ImagesAPIResult,
        *,
        kind: PosterKind,
        size: str,
        index: int,
    ) -> GeneratedImagePayload:
        width, height = parse_size(size)
        dims = image_dimensions_from_bytes(result.bytes_data)
        if dims:
            width, height = dims

        return GeneratedImagePayload(
            kind=kind,
            bytes_data=result.bytes_data,
            mime_type=result.mime_type,
            width=width,
            height=height,
            variant_label=f"v{index}",
            provider_output_json=result.provider_output_json,
        )

    def _build_prompt(self, poster: PosterGenerationInput, kind: PosterKind, size: str, settings: Any) -> str:
        copy_mode = poster.copy_prompt_mode == "copy"
        template = settings.prompt_poster_image_template if copy_mode else settings.prompt_poster_image_edit_template
        return render_prompt_template(
            template,
            {
                "product_name": poster.product_name,
                "category": poster.category or "",
                "price": poster.price or "",
                "source_note": poster.source_note or "",
                "instruction": poster.instruction or "Free image generation.",
                "context_block": self._build_context_block(poster),
                "reference_policy": (
                    settings.prompt_poster_image_reference_policy if poster_has_reference_input(poster) else ""
                ),
                "visible_text_language_hint": poster.visible_text_language_hint or "",
                "size": size,
                "kind": kind.value,
                "kind_label": "main image" if kind == PosterKind.MAIN_IMAGE else "promotional poster",
                "kind_requirements": self._build_kind_requirements(
                    kind,
                    visible_text_language_hint=poster.visible_text_language_hint,
                ),
            },
        )

    def _build_context_block(self, poster: PosterGenerationInput) -> str:
        return build_poster_context_block(poster)

    def _build_kind_requirements(self, kind: PosterKind, *, visible_text_language_hint: str | None = None) -> str:
        return image_visible_text_requirements(kind, visible_text_language_hint=visible_text_language_hint)

    def _build_reference_images_from_poster(self, poster: PosterGenerationInput) -> list[ImagesReferenceImage]:
        return [
            ImagesReferenceImage(
                bytes_data=reference.bytes_data,
                mime_type=reference.mime_type,
                filename=reference.filename or f"reference-{index}.png",
            )
            for index, reference in enumerate(build_responses_reference_images_from_poster(poster), start=1)
        ]
