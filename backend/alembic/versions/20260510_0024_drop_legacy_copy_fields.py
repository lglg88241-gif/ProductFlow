"""drop legacy copy fields

Revision ID: 20260510_0024
Revises: 20260509_0023
Create Date: 2026-05-10 00:00:00.000000
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "20260510_0024"
down_revision = "20260509_0023"
branch_labels = None
depends_on = None


LEGACY_COLUMNS = (
    "title",
    "selling_points",
    "poster_headline",
    "cta",
    "model_title",
    "model_selling_points",
    "model_poster_headline",
    "model_cta",
)


def upgrade() -> None:
    connection = op.get_bind()
    copy_sets = sa.table(
        "copy_sets",
        sa.column("id", sa.String()),
        sa.column("title", sa.Text()),
        sa.column("selling_points", sa.JSON()),
        sa.column("poster_headline", sa.Text()),
        sa.column("cta", sa.Text()),
        sa.column("structured_payload", sa.JSON()),
        sa.column("model_title", sa.Text()),
        sa.column("model_selling_points", sa.JSON()),
        sa.column("model_poster_headline", sa.Text()),
        sa.column("model_cta", sa.Text()),
        sa.column("model_structured_payload", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(
            copy_sets.c.id,
            copy_sets.c.title,
            copy_sets.c.selling_points,
            copy_sets.c.poster_headline,
            copy_sets.c.cta,
            copy_sets.c.structured_payload,
            copy_sets.c.model_title,
            copy_sets.c.model_selling_points,
            copy_sets.c.model_poster_headline,
            copy_sets.c.model_cta,
            copy_sets.c.model_structured_payload,
        )
    ).mappings()
    for row in rows:
        structured_payload = row["structured_payload"] or _payload_from_legacy_fields(
            title=row["title"],
            selling_points=row["selling_points"],
            poster_headline=row["poster_headline"],
            cta=row["cta"],
        )
        model_structured_payload = row["model_structured_payload"] or _payload_from_legacy_fields(
            title=row["model_title"],
            selling_points=row["model_selling_points"],
            poster_headline=row["model_poster_headline"],
            cta=row["model_cta"],
        )
        connection.execute(
            copy_sets.update()
            .where(copy_sets.c.id == row["id"])
            .values(
                structured_payload=structured_payload,
                model_structured_payload=model_structured_payload,
            )
        )

    with op.batch_alter_table("copy_sets") as batch_op:
        for column_name in LEGACY_COLUMNS:
            batch_op.drop_column(column_name)


def downgrade() -> None:
    with op.batch_alter_table("copy_sets") as batch_op:
        batch_op.add_column(sa.Column("title", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("selling_points", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("poster_headline", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("cta", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("model_title", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("model_selling_points", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("model_poster_headline", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("model_cta", sa.Text(), nullable=False, server_default=""))

    connection = op.get_bind()
    copy_sets = sa.table(
        "copy_sets",
        sa.column("id", sa.String()),
        sa.column("structured_payload", sa.JSON()),
        sa.column("model_structured_payload", sa.JSON()),
        sa.column("title", sa.Text()),
        sa.column("selling_points", sa.JSON()),
        sa.column("poster_headline", sa.Text()),
        sa.column("cta", sa.Text()),
        sa.column("model_title", sa.Text()),
        sa.column("model_selling_points", sa.JSON()),
        sa.column("model_poster_headline", sa.Text()),
        sa.column("model_cta", sa.Text()),
    )
    rows = connection.execute(
        sa.select(copy_sets.c.id, copy_sets.c.structured_payload, copy_sets.c.model_structured_payload)
    ).mappings()
    for row in rows:
        fields = _legacy_fields_from_payload(row["structured_payload"])
        model_fields = _legacy_fields_from_payload(row["model_structured_payload"])
        connection.execute(
            copy_sets.update()
            .where(copy_sets.c.id == row["id"])
            .values(
                title=fields["title"],
                selling_points=fields["selling_points"],
                poster_headline=fields["poster_headline"],
                cta=fields["cta"],
                model_title=model_fields["title"],
                model_selling_points=model_fields["selling_points"],
                model_poster_headline=model_fields["poster_headline"],
                model_cta=model_fields["cta"],
            )
        )


def _payload_from_legacy_fields(
    *,
    title: Any,
    selling_points: Any,
    poster_headline: Any,
    cta: Any,
) -> dict[str, Any]:
    clean_title = _text(title)
    clean_points = _text_list(selling_points)
    clean_headline = _text(poster_headline)
    clean_cta = _text(cta)
    blocks: list[dict[str, Any]] = []
    if clean_title:
        blocks.append({"id": "title", "role": "headline", "label": "标题", "text": clean_title, "priority": 1})
    for index, point in enumerate(clean_points, start=1):
        blocks.append(
            {
                "id": f"selling-point-{index}",
                "role": "selling_point",
                "label": f"卖点 {index}",
                "text": point,
                "priority": index + 1,
            }
        )
    if clean_headline and clean_headline != clean_title:
        blocks.append({"id": "poster-headline", "role": "headline", "label": "海报标题", "text": clean_headline})
    if clean_cta:
        blocks.append({"id": "cta", "role": "cta", "label": "CTA", "text": clean_cta})
    summary = clean_headline or clean_title or (clean_points[0] if clean_points else "文案")
    if not blocks:
        blocks.append({"id": "summary", "role": "summary", "label": "摘要", "text": summary})
    return {
        "version": 2,
        "summary": summary,
        "content": {"kind": "blocks", "blocks": blocks},
        "visual_guidance": None,
    }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _legacy_fields_from_payload(raw_payload: Any) -> dict[str, Any]:
    payload = _json_object(raw_payload)
    summary = _text(payload.get("summary"))
    content = _json_object(payload.get("content"))
    texts: list[str] = []
    title = ""
    headline = ""
    poster_headline = ""
    cta = ""
    points: list[str] = []
    if content.get("kind") == "blocks":
        for block in content.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            text = _text(block.get("text"))
            if not text:
                continue
            role = _text(block.get("role"))
            block_id = _text(block.get("id"))
            label = _text(block.get("label"))
            texts.append(text)
            if block_id == "title" and not title:
                title = text
            if block_id == "poster-headline" or label == "海报标题":
                poster_headline = poster_headline or text
            elif role in {"headline", "title"} and not headline:
                headline = text
            elif role == "cta" and not cta:
                cta = text
            elif role == "selling_point":
                points.append(text)
    elif content.get("kind") == "freeform":
        text = _text(content.get("text"))
        if text:
            texts.append(text)
    elif content.get("kind") == "layout_brief":
        for section in content.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_title = _text(section.get("title"))
            section_body = _text(section.get("body"))
            if section_title:
                texts.append(section_title)
            if section_body:
                texts.append(section_body)
            for item in section.get("items") or []:
                if not isinstance(item, dict):
                    continue
                text = _text(item.get("text"))
                if not text:
                    continue
                texts.append(text)
                if _text(item.get("role")) == "cta" and not cta:
                    cta = text
    visual_guidance = _json_object(payload.get("visual_guidance"))
    visual_message = _text(visual_guidance.get("main_message"))
    title = texts[0] if texts else summary
    if not points:
        points = [text for text in texts[1:] if text != cta][:5]
    title = title or (texts[0] if texts else summary)
    return {
        "title": title[:500],
        "selling_points": [point[:500] for point in points[:5]],
        "poster_headline": (visual_message or poster_headline or headline or summary or title)[:500],
        "cta": cta[:300],
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
