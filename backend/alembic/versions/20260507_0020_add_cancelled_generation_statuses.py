"""add cancelled generation statuses

Revision ID: 20260507_0020
Revises: 20260430_0019
Create Date: 2026-05-07
"""

from __future__ import annotations

from alembic import op

revision = "20260507_0020"
down_revision = "20260430_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'cancelled'")
            op.execute("ALTER TYPE workflowrunstatus ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    pass
