"""add designer asset library

Revision ID: 20260906_0032
Revises: 20260906_0031
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260906_0032"
down_revision = "20260906_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_library",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("vision_tags_json", sa.JSON(), nullable=True),
        sa.Column("template_profile_json", sa.JSON(), nullable=True),
        sa.Column("agent_session_id", sa.String(length=36), nullable=True),
        sa.Column("image_session_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('template', 'reference', 'output', 'brand')", name="ck_asset_library_kind"),
        sa.CheckConstraint("source IN ('upload', 'generated', 'builtin')", name="ck_asset_library_source"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_library_kind_created", "asset_library", ["kind", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_asset_library_kind_created", table_name="asset_library")
    op.drop_table("asset_library")
