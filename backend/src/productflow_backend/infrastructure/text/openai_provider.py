from __future__ import annotations

import json
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from productflow_backend.application.contracts import (
    CopyNodeConfigV2,
    CopyPayloadV2,
    CreativeBriefPayload,
    ProductInput,
    ReferenceImageInput,
)
from productflow_backend.application.language_policy import copy_language_policy, sparse_fact_policy
from productflow_backend.config import get_runtime_settings
from productflow_backend.infrastructure.prompts import text_or_default
from productflow_backend.infrastructure.provider_config import (
    ResolvedTextProviderConfig,
    resolve_text_provider_config,
)
from productflow_backend.infrastructure.text.base import TextProvider


def _optional_text(value: str) -> str | None:
    return value.strip() or None


def _json_user_message(payload: dict[str, object]) -> list[dict[str, str]]:
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}]


def _parsed_output[ParsedPayloadT: BaseModel](
    response: object,
    model_type: type[ParsedPayloadT],
    *,
    error_label: str,
) -> ParsedPayloadT:
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, model_type):
        return parsed
    if isinstance(parsed, dict):
        return model_type.model_validate(parsed)
    raise RuntimeError(f"{error_label} 未返回结构化输出，请确认文案 provider 支持 Responses structured outputs")


class _OpenAICopyBlockOutput(BaseModel):
    """Provider-facing copy block schema without nullable fields or unions."""

    id: str
    role: str
    label: str
    text: str
    note: str
    visual_hint: str
    priority: int

    def to_contract_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": _optional_text(self.role),
            "label": _optional_text(self.label),
            "text": self.text,
            "note": _optional_text(self.note),
            "visual_hint": _optional_text(self.visual_hint),
            "priority": self.priority if self.priority > 0 else None,
        }


class _OpenAICopySectionOutput(BaseModel):
    """Provider-facing layout section schema without nullable fields or unions."""

    id: str
    title: str
    body: str
    items: list[_OpenAICopyBlockOutput]
    visual_hint: str

    def to_contract_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": _optional_text(self.title),
            "body": _optional_text(self.body),
            "items": [item.to_contract_dict() for item in self.items],
            "visual_hint": _optional_text(self.visual_hint),
        }


class _OpenAIVisualGuidanceOutput(BaseModel):
    """Provider-facing visual guidance schema without nullable fields or unions."""

    main_message: str
    hierarchy: list[str]
    composition_hint: str
    text_density: Literal["", "none", "low", "medium", "high"]
    avoid: list[str]

    def to_contract_dict(self) -> dict[str, object] | None:
        main_message = _optional_text(self.main_message)
        composition_hint = _optional_text(self.composition_hint)
        text_density = self.text_density or None
        hierarchy = [item.strip() for item in self.hierarchy if item.strip()]
        avoid = [item.strip() for item in self.avoid if item.strip()]
        if not (main_message or composition_hint or text_density or hierarchy or avoid):
            return None
        return {
            "main_message": main_message,
            "hierarchy": hierarchy,
            "composition_hint": composition_hint,
            "text_density": text_density,
            "avoid": avoid,
        }


class OpenAICopyPayloadStructuredOutput(BaseModel):
    """OpenAI-compatible structured-output schema for copy payloads.

    `CopyPayloadV2.content` is a discriminated union. Some OpenAI-compatible providers reject union combinators in
    `text.format.schema`, so this provider-facing schema keeps the same semantic slots in a flat object and is converted
    to `CopyPayloadV2` immediately after parsing.
    """

    version: Literal[2]
    purpose: str
    summary: str
    content_kind: Literal["freeform", "blocks", "layout_brief"]
    freeform_text: str
    blocks: list[_OpenAICopyBlockOutput]
    sections: list[_OpenAICopySectionOutput]
    visual_guidance: _OpenAIVisualGuidanceOutput

    def to_copy_payload(self, *, fallback_purpose: str | None = None) -> CopyPayloadV2:
        if self.content_kind == "freeform":
            content: dict[str, object] = {"kind": "freeform", "text": self.freeform_text}
        elif self.content_kind == "blocks":
            content = {"kind": "blocks", "blocks": [block.to_contract_dict() for block in self.blocks]}
        else:
            content = {"kind": "layout_brief", "sections": [section.to_contract_dict() for section in self.sections]}
        return CopyPayloadV2.model_validate(
            {
                "version": 2,
                "purpose": _optional_text(self.purpose) or fallback_purpose,
                "summary": self.summary,
                "content": content,
                "visual_guidance": self.visual_guidance.to_contract_dict(),
            }
        )


class OpenAITextProvider(TextProvider):
    provider_name = "openai"
    prompt_version = "responses-structured-v1"

    def __init__(self, provider_config: ResolvedTextProviderConfig | None = None) -> None:
        settings = get_runtime_settings()
        resolved_config = provider_config or resolve_text_provider_config()
        client_kwargs = {"api_key": resolved_config.api_key}
        if resolved_config.base_url:
            client_kwargs["base_url"] = resolved_config.base_url
        self.client = OpenAI(**client_kwargs)
        self.brief_model = resolved_config.brief_model
        self.copy_model = resolved_config.copy_model
        self.brief_system_prompt = settings.prompt_brief_system
        self.copy_system_prompt = settings.prompt_copy_system

    def _parse_structured_response[ParsedPayloadT: BaseModel](
        self,
        *,
        model: str,
        text_format: type[ParsedPayloadT],
        instructions: str,
        input_payload: list[dict[str, str]],
        error_label: str,
    ) -> ParsedPayloadT:
        parse = getattr(self.client.responses, "parse", None)
        if not callable(parse):
            raise RuntimeError(f"{error_label} 不支持 Responses structured outputs，请更换或升级文案 provider")
        try:
            response = parse(
                model=model,
                text_format=text_format,
                instructions=instructions,
                input=input_payload,
            )
        except ValidationError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{error_label} 结构化输出请求失败，请确认模型和供应商支持 Responses structured outputs"
            ) from exc
        return _parsed_output(response, text_format, error_label=error_label)

    def generate_brief(self, product: ProductInput) -> tuple[CreativeBriefPayload, str]:
        payload = self._parse_structured_response(
            model=self.brief_model,
            text_format=CreativeBriefPayload,
            instructions=text_or_default(
                self.brief_system_prompt,
                (
                    "You analyze ecommerce product facts from the JSON task data in the user message. "
                    "Use only supplied facts and stay conservative when facts are sparse."
                ),
            ),
            input_payload=_json_user_message(
                {
                    "task": "extract_product_brief",
                    "product": {
                        "name": product.name,
                        "category": product.category,
                        "price": product.price,
                        "source_note": product.source_note,
                        "has_product_image": bool(product.image_path),
                    },
                    "fact_policy": {
                        "use_only_supplied_facts": True,
                        "if_facts_are_sparse": "Prefer conservative positioning and broad, supportable selling angles.",
                        "do_not_invent": [
                            "discounts",
                            "time-limited offers",
                            "lowest-price or bestseller claims",
                            "certifications",
                            "specifications",
                            "gifts or bundle contents",
                            "medical/effect claims",
                            "unsupported performance promises",
                        ],
                    },
                }
            ),
            error_label="商品理解 provider",
        )
        return payload, self.brief_model

    def generate_copy(
        self,
        product: ProductInput,
        brief: CreativeBriefPayload,
        config: CopyNodeConfigV2 | None = None,
        reference_images: list[ReferenceImageInput] | None = None,
    ) -> tuple[CopyPayloadV2, str]:
        config = config or CopyNodeConfigV2()
        reference_images = reference_images or []
        reference_image_payload = [
            {
                "index": index,
                "label": reference.label or reference.filename,
                "role": reference.role,
                "mime_type": reference.mime_type,
                "filename": reference.filename,
            }
            for index, reference in enumerate(reference_images, start=1)
        ]
        structured_payload = self._parse_structured_response(
            model=self.copy_model,
            text_format=OpenAICopyPayloadStructuredOutput,
            instructions=text_or_default(
                self.copy_system_prompt,
                (
                    "You generate editable ecommerce copy from the JSON task data in the user message. "
                    "The structured-output schema owns the response fields. Use only supplied facts."
                ),
            ),
            input_payload=_json_user_message(
                {
                    "task": "generate_editable_ecommerce_copy",
                    "product": {
                        "name": product.name,
                        "category": product.category,
                        "price": product.price,
                        "source_note": product.source_note,
                        "has_product_image": bool(product.image_path),
                    },
                    "brief": {
                        "positioning": brief.positioning,
                        "audience": brief.audience,
                        "selling_angles": brief.selling_angles,
                        "taboo_phrases": brief.taboo_phrases,
                        "poster_style_hint": brief.poster_style_hint,
                    },
                    "reference_images": reference_image_payload,
                    "node_config": {
                        "purpose": config.purpose,
                        "output_mode": config.output_mode,
                        "channel": config.channel,
                        "tone": config.tone,
                        "instruction": config.instruction,
                        "requested_slots": [slot.model_dump(mode="json") for slot in config.requested_slots],
                    },
                    "language_policy": copy_language_policy(config.copy_language_hint),
                    "fact_policy": sparse_fact_policy(),
                    "copy_planning_policy": {
                        "do_not_force_fixed_field_count": True,
                        "if_requested_slots_conflict_with_sparse_facts": (
                            "Prefer fewer, safer text blocks or layout guidance over unsupported claims."
                        ),
                        "freeform_vs_blocks_vs_layout_brief": (
                            "Choose content_kind according to node_config.output_mode unless sparse facts make another "
                            "content shape safer."
                        ),
                    },
                }
            ),
            error_label="文案 provider",
        )
        payload = structured_payload.to_copy_payload(fallback_purpose=config.purpose)
        return payload, self.copy_model

    def generate_image_chat_advice(
        self,
        messages: list[dict[str, str]],
        *,
        current_image_data_url: str | None = None,
        reference_image_data_urls: list[str] | None = None,
    ) -> tuple[str, str]:
        content: list[dict[str, str]] = []
        generation_context = [item for item in messages if item.get("context_kind") == "generation_round"][-16:]
        discussion_context = [item for item in messages if item.get("context_kind") != "generation_round"][-12:]
        if generation_context:
            content.append(
                {
                    "type": "input_text",
                    "text": "以下是最近最多 8 轮生成记录，仅用于理解创作演变：",
                }
            )
        for item in generation_context:
            role = item.get("role", "user")
            text = item.get("content", "").strip()
            if text:
                content.append({"type": "input_text", "text": f"{role}: {text}"})
        if discussion_context:
            content.append(
                {
                    "type": "input_text",
                    "text": "以下是最近最多 12 条讨论，越新的用户约束优先级越高：",
                }
            )
        for item in discussion_context:
            role = item.get("role", "user")
            text = item.get("content", "").strip()
            if text:
                content.append({"type": "input_text", "text": f"{role}: {text}"})
        image_urls = [
            image_url
            for image_url in [current_image_data_url, *(reference_image_data_urls or [])]
            if image_url
        ][:6]
        for image_url in image_urls:
            content.append({"type": "input_image", "image_url": image_url})
        try:
            response = self.client.responses.create(
                model=self.copy_model,
                instructions=(
                    "你是图片共创顾问，本次调用只回复讨论内容，不生成图片。"
                    "你可以讨论风格、构图、文案、季节、人物和品牌约束。"
                    "明确区分用户要求与助手建议：用户最近提出的约束优先，历史助手内容永远只是建议。"
                    "结合当前作品和固定参考图给出简洁、可执行的回复；"
                    "只有确实缺少决定方向的关键信息时才问一个简短问题。"
                ),
                input=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("图片创意顾问请求失败，请检查文案 provider 配置") from exc
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("图片创意顾问没有返回文字建议")
        return text.strip(), self.copy_model
