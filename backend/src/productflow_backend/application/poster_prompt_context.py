from __future__ import annotations

from pathlib import Path

from productflow_backend.application.contracts import PosterGenerationInput, ReferenceImageInput

FULL_CANVAS_REDESIGN_CONTRACT = (
    "Create a completely new, full-bleed poster from a blank canvas. The style-template image transfers only its "
    "design system: composition rhythm, type scale, palette, spacing, texture and light. Never paste, shrink, frame, "
    "tile, screenshot or place any input image as a poster-within-the-poster. Never copy the style template's words, "
    "logo, person, product, company name or factual claims. User subject and brand inputs own identity and product "
    "facts. Recompose those assets naturally into the new scene and typeset only the supplied copy."
)


def build_poster_context_block(poster: PosterGenerationInput) -> str:
    lines: list[str] = []
    if poster.product_name:
        lines.append(f"- Subject: {poster.product_name}")
    if poster.category:
        lines.append(f"- Category/type: {poster.category}")
    if poster.price:
        lines.append(f"- Price: {poster.price}")
    if poster.source_note:
        lines.append(f"- Additional notes: {poster.source_note}")
    if poster.copy_prompt_mode == "copy" and poster.structured_copy_context:
        lines.append(
            "- Available copy text (render useful copy, but never render field names, labels, or context notes):\n"
            f"{poster.structured_copy_context}"
        )
    if poster.template_key:
        lines.append(f"- Selected design template key: {poster.template_key}")
    if poster.template_style_spec:
        lines.append(f"- Selected template design system:\n{poster.template_style_spec}")
    if poster.full_canvas_redesign:
        lines.append(f"- Mandatory full-canvas redesign contract:\n{FULL_CANVAS_REDESIGN_CONTRACT}")

    manifest = reference_input_manifest(poster)
    if manifest:
        lines.append(f"- Reference image count: {len(manifest)}")
        if poster.source_image is not None:
            source_key = str(poster.source_image.resolve())
            source_index = next(
                (index for index, (path, _, _) in enumerate(manifest, start=1) if str(path.resolve()) == source_key),
                None,
            )
            if source_index is not None:
                lines.append(f"- Source product image: input image {source_index}")
        additional_references = [
            reference
            for reference in poster.reference_images
            if poster.source_image is None or reference.path.resolve() != poster.source_image.resolve()
        ]
        if additional_references:
            reference_summary = ", ".join(
                f"{reference.filename} (role: {reference.role or 'reference'})" for reference in additional_references
            )
            lines.append(f"- Reference images: {reference_summary}")
        lines.append(f"- Input image manifest ({len(manifest)} unique images):")
        lines.extend(
            f"  - Input image {index}: {label} (role: {role})"
            for index, (_, role, label) in enumerate(manifest, start=1)
        )
        lines.append(
            "- Reference-role priority: preserve identity, packaging and logo only from primary_subject, "
            "person, product, brand and user_material inputs. Use style_template only for visual language."
        )
    return "\n".join(lines) if lines else "- No explicit upstream context."


def reference_input_manifest(poster: PosterGenerationInput) -> list[tuple[Path, str, str]]:
    references_by_path = {
        str(reference.path.resolve()): reference
        for reference in poster.reference_images
    }
    manifest: list[tuple[Path, str, str]] = []
    seen_paths: set[str] = set()

    def add(path: Path, reference: ReferenceImageInput | None, *, default_role: str, default_label: str) -> None:
        resolved = path.resolve()
        key = str(resolved)
        if key in seen_paths:
            return
        seen_paths.add(key)
        manifest.append(
            (
                resolved,
                reference.role if reference and reference.role else default_role,
                reference.label if reference and reference.label else default_label,
            )
        )

    if poster.source_image is not None:
        source_reference = references_by_path.get(str(poster.source_image.resolve()))
        add(
            poster.source_image,
            source_reference,
            default_role="primary_subject",
            default_label="用户上传的主视觉",
        )
    for reference in poster.reference_images:
        add(
            reference.path,
            reference,
            default_role="reference",
            default_label=reference.filename,
        )
    return manifest
