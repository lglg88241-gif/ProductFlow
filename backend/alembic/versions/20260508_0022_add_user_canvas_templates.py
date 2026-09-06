"""add user canvas templates

Revision ID: 20260508_0022
Revises: 20260507_0021
Create Date: 2026-05-08
"""

import sqlalchemy as sa

from alembic import op

revision = "20260508_0022"
down_revision = "20260507_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_canvas_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("template_json", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_user_canvas_templates_archived_at", "user_canvas_templates", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_user_canvas_templates_archived_at", table_name="user_canvas_templates")
    op.drop_table("user_canvas_templates")
