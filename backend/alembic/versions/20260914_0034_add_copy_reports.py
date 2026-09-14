"""add copy reports table

Revision ID: 20260914_0034
Revises: 20260914_0033
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260914_0034"
down_revision = "20260914_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Agent 文案报告落库：markdown 全文 + 会话溯源（会话删除时置空保留报告）。"""
    op.create_table(
        "copy_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_session_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.id"],
            name="fk_copy_reports_agent_session_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_copy_reports_agent_session_created",
        "copy_reports",
        ["agent_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_copy_reports_agent_session_created", table_name="copy_reports")
    op.drop_table("copy_reports")
