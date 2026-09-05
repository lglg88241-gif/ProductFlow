from pathlib import Path

from productflow_backend.application.contracts import PosterGenerationInput, ReferenceImageInput
from productflow_backend.application.poster_prompt_context import (
    FULL_CANVAS_REDESIGN_CONTRACT,
    build_poster_context_block,
    reference_input_manifest,
)


def _reference(path: Path, *, role: str, label: str) -> ReferenceImageInput:
    return ReferenceImageInput(
        path=path,
        mime_type="image/jpeg",
        filename=path.name,
        role=role,
        label=label,
    )


def test_moments_prompt_contains_template_design_system_and_full_canvas_contract(tmp_path: Path) -> None:
    subject = tmp_path / "subject.jpg"
    style_template = tmp_path / "template.jpg"
    poster = PosterGenerationInput(
        product_name="新品牌",
        template_key="moments-beauty-clean-v1",
        template_style_spec="Deep emerald palette; oversized editorial typography.",
        full_canvas_redesign=True,
        source_image=subject,
        reference_images=[
            _reference(subject, role="primary_subject", label="用户主视觉"),
            _reference(style_template, role="style_template", label="风格样板"),
        ],
    )

    context = build_poster_context_block(poster)

    assert "moments-beauty-clean-v1" in context
    assert "Deep emerald palette; oversized editorial typography." in context
    assert FULL_CANVAS_REDESIGN_CONTRACT in context
    assert "Never paste, shrink, frame, tile, screenshot or place any input image" in context


def test_style_template_is_last_and_controls_only_visual_language(tmp_path: Path) -> None:
    subject = tmp_path / "subject.jpg"
    material = tmp_path / "material.jpg"
    style_template = tmp_path / "template.jpg"
    poster = PosterGenerationInput(
        product_name="新品牌",
        source_image=subject,
        reference_images=[
            _reference(subject, role="primary_subject", label="用户主视觉"),
            _reference(material, role="user_material", label="用户附加素材"),
            _reference(style_template, role="style_template", label="内置风格样板"),
        ],
    )

    manifest = reference_input_manifest(poster)
    context = build_poster_context_block(poster)

    assert [role for _, role, _ in manifest] == ["primary_subject", "user_material", "style_template"]
    assert [path for path, _, _ in manifest] == [subject, material, style_template]
    assert "Use style_template only for visual language" in context
    assert "preserve identity, packaging and logo only from primary_subject" in context


def test_source_image_is_not_duplicated_in_reference_manifest(tmp_path: Path) -> None:
    subject = tmp_path / "subject.jpg"
    poster = PosterGenerationInput(
        product_name="新品牌",
        source_image=subject,
        reference_images=[_reference(subject, role="primary_subject", label="用户主视觉")],
    )

    manifest = reference_input_manifest(poster)

    assert manifest == [(subject, "primary_subject", "用户主视觉")]
