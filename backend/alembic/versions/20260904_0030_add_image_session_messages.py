"""add persisted image-session discussion messages

Revision ID: 20260904_0030
Revises: 20260627_0029
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "20260904_0030"
down_revision = "20260627_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_session_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_image_session_messages_role"),
        sa.ForeignKeyConstraint(["session_id"], ["image_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_image_session_messages_session_created",
        "image_session_messages",
        ["session_id", "created_at", "id"],
    )

    # Existing installations bootstrapped openai_images from the old image
    # default. Preserve every explicit custom model and migrate only that
    # legacy default to the model required by free-form image sessions.
    bindings = sa.table(
        "provider_bindings",
        sa.column("id", sa.String(length=36)),
        sa.column("provider_kind", sa.String(length=40)),
        sa.column("model_settings_json", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(bindings.c.id, bindings.c.model_settings_json).where(
            bindings.c.provider_kind == "openai_images"
        )
    ).all()
    for binding_id, raw_settings in rows:
        settings = raw_settings
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (TypeError, ValueError):
                settings = None
        if not isinstance(settings, dict) or settings.get("model") != "gpt-5.4":
            continue
        updated = dict(settings)
        updated["model"] = "gpt-image-2"
        connection.execute(
            bindings.update()
            .where(bindings.c.id == binding_id)
            .values(model_settings_json=updated)
        )


def downgrade() -> None:
    op.drop_index("ix_image_session_messages_session_created", table_name="image_session_messages")
    op.drop_table("image_session_messages")
