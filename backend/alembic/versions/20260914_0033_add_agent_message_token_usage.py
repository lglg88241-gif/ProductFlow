"""add agent message token usage columns

Revision ID: 20260914_0033
Revises: 20260906_0032
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260914_0033"
down_revision = "20260906_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """agent_messages 增加 token 用量三列；历史数据无回填，保持 NULL。"""
    op.add_column("agent_messages", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_messages", sa.Column("completion_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_messages", sa.Column("total_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_messages", "total_tokens")
    op.drop_column("agent_messages", "completion_tokens")
    op.drop_column("agent_messages", "prompt_tokens")
