from __future__ import annotations

from productflow_backend.application.contracts import (
    BlocksCopyContent,
    CopyBlock,
    CopyNodeConfigV2,
    CopyPayloadV2,
    CreativeBriefPayload,
    ProductInput,
    ReferenceImageInput,
    VisualGuidance,
)
from productflow_backend.infrastructure.text.base import TextProvider


class MockTextProvider(TextProvider):
    provider_name = "mock"

    def generate_brief(self, product: ProductInput) -> tuple[CreativeBriefPayload, str]:
        category = product.category or "通用电商"
        note_hint = f"，重点参考：{product.source_note[:48]}" if product.source_note else ""
        brief = CreativeBriefPayload(
            positioning=f"{category}场景下的实用型商品{note_hint}",
            audience="追求性价比、希望快速了解卖点的电商消费者",
            selling_angles=[
                "突出核心用途，先让人知道买来能解决什么问题",
                "强调到手直观收益，不堆空泛形容词",
                "语言更接近淘宝主图与促销海报风格",
            ],
            taboo_phrases=["全网最低", "包治百病", "绝对有效"],
            poster_style_hint="白底主图 + 强调主卖点的红色促销信息",
        )
        return brief, "mock-brief-v1"

    def generate_copy(
        self,
        product: ProductInput,
        brief: CreativeBriefPayload,
        config: CopyNodeConfigV2,
        reference_images: list[ReferenceImageInput] | None = None,
    ) -> tuple[CopyPayloadV2, str]:
        category_prefix = f"{product.category} " if product.category else ""
        price_line = f" 参考价 {product.price}" if product.price else ""
        note_line = f"，结合描述：{product.source_note[:36]}" if product.source_note else ""
        instruction_line = f"，本轮方向：{config.instruction[:32]}" if config.instruction else ""
        reference_images = reference_images or []
        reference_hint = ""
        if reference_images:
            first_reference = reference_images[0]
            label = first_reference.label or first_reference.filename
            role = first_reference.role or "参考图"
            reference_hint = f"，参考{role}：{label}"
        title = f"{category_prefix}{product.name}｜实用好上手，店铺主推更省心"
        points = [
            f"核心用途更清楚：{product.name}一眼看懂重点{note_line}{reference_hint}",
            "展示更直接，适合主图、详情页或促销素材快速承接",
            (
                f"语言偏{config.tone or '转化清晰'}，适合"
                f"{config.channel or '电商'}场景{price_line}{instruction_line}"
            ).strip(),
        ]
        copy = CopyPayloadV2(
            purpose=config.purpose,
            summary=title,
            content=BlocksCopyContent(
                blocks=[
                    CopyBlock(id="headline", role="headline", label="主信息", text=title, priority=1),
                    *[
                        CopyBlock(
                            id=f"point-{index}",
                            role="selling_point",
                            label=f"卖点 {index}",
                            text=point,
                            visual_hint="可作为画面标注或图标旁短说明",
                            priority=index + 1,
                        )
                        for index, point in enumerate(points, start=1)
                    ],
                ]
            ),
            visual_guidance=VisualGuidance(
                main_message=title,
                hierarchy=["商品主体", "核心卖点", "补充说明"],
                composition_hint=brief.poster_style_hint,
                text_density="medium",
                avoid=brief.taboo_phrases,
            ),
        )
        return copy, "mock-copy-v2"
