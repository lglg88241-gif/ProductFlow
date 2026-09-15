"""add session list indexes

Revision ID: 20260915_0035
Revises: 20260914_0034
"""

from __future__ import annotations

import sqlalchemy as sa  # noqa: F401

from alembic import op

revision = "20260915_0035"
down_revision = "20260914_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """列表接口排序索引：会话类列表按 updated_at 降序，报告列表按 created_at 降序。"""
    op.create_index("ix_image_sessions_updated_at", "image_sessions", ["updated_at"])
    op.create_index("ix_agent_sessions_updated_at", "agent_sessions", ["updated_at"])
    op.create_index("ix_copy_reports_created_at", "copy_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_copy_reports_created_at", table_name="copy_reports")
    op.drop_index("ix_agent_sessions_updated_at", table_name="agent_sessions")
    op.drop_index("ix_image_sessions_updated_at", table_name="image_sessions")
