from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from productflow_backend.application.contracts import ReferenceImageInput

MOMENTS_TEMPLATE_PREFIX = "moments-beauty-"
TEMPLATE_METADATA_CONFIG_KEY = "_canvas_template"


@dataclass(frozen=True, slots=True)
class MomentsPosterTemplateDesign:
    key: str
    title: str
    description: str
    reference_filename: str
    style_spec: str

    @property
    def reference_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "moments_templates"
            / self.reference_filename
        )

    def reference_input(self) -> ReferenceImageInput:
        return ReferenceImageInput(
            path=self.reference_path,
            mime_type="image/jpeg",
            filename=self.reference_filename,
            role="style_template",
            label=f"内置样板：{self.title}",
        )


MOMENTS_POSTER_TEMPLATE_DESIGNS: dict[str, MomentsPosterTemplateDesign] = {
    "moments-beauty-clean-v1": MomentsPosterTemplateDesign(
        key="moments-beauty-clean-v1",
        title="深墨绿模特大字",
        description="深墨绿与黑色满版、模特主视觉、荧光笔刷英文和超大中文标题。",
        reference_filename="emerald-model-editorial.jpg",
        style_spec=(
            "Full-bleed premium beauty campaign poster on a deep emerald-to-black background. "
            "Use one dominant fashion or beauty subject through the middle of the canvas, an oversized vivid "
            "green Latin brush word behind the subject, and two lines of very large white Chinese display type "
            "overlapping the subject without covering the face. Add one restrained cyan-white light sweep across "
            "the lower middle. Keep a small brand line at top left, a concise fact at top right, and 2-4 compact "
            "supporting lines near the bottom. Editorial, dramatic, sharp and expensive; no cards or app UI."
        ),
    ),
    "moments-beauty-neon-v1": MomentsPosterTemplateDesign(
        key="moments-beauty-neon-v1",
        title="黑粉高冲击爆字",
        description="纯黑满版、超大粉色中文爆字、荧光绿英文叠排和强留白。",
        reference_filename="black-pink-impact-type.jpg",
        style_spec=(
            "Full-bleed typographic campaign poster on matte black. Fill the upper 45-52 percent with extremely "
            "large hot-pink Chinese display characters, intentionally cropped at selected canvas edges. Overlay "
            "one acid-green condensed English phrase and one short yellow accent line across the headline. Preserve "
            "a bold area of black negative space in the middle, then place 2-3 concise green supporting lines in the "
            "lower quarter with tiny editorial microtype accents. Graphic, rebellious and high-impact; no person is "
            "required unless the user explicitly supplies one; no panels, cards, buttons or price badges."
        ),
    ),
    "moments-beauty-luxe-v1": MomentsPosterTemplateDesign(
        key="moments-beauty-luxe-v1",
        title="紫黑高级活动战报",
        description="紫黑舞台光感、书法与黑体混排、权益信息带和活动战报层级。",
        reference_filename="purple-black-event-report.jpg",
        style_spec=(
            "Full-bleed black-purple event poster with a subtle stage or architectural background, violet grain, "
            "and controlled floor light. Build the upper half from an oversized Chinese headline that mixes bold "
            "block type with expressive white-to-violet brush calligraphy. Use a compact brand line in the top left "
            "and one result or date fact in the top right. Organize the lower half into 3-6 thin violet editorial "
            "information bands with clear alignment; they are printed information rows, never clickable UI. Finish "
            "with a restrained violet light arc at the bottom. Dense but premium, legible and campaign-ready."
        ),
    ),
}


def moments_template_design(template_key: str | None) -> MomentsPosterTemplateDesign | None:
    if not template_key:
        return None
    return MOMENTS_POSTER_TEMPLATE_DESIGNS.get(template_key)


def canvas_template_key_from_config(config: dict[str, Any]) -> str | None:
    metadata = config.get(TEMPLATE_METADATA_CONFIG_KEY)
    if not isinstance(metadata, dict):
        return None
    template_key = metadata.get("template_key")
    if not isinstance(template_key, str):
        return None
    normalized = template_key.strip()
    return normalized or None
