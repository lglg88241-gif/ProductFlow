"""add workflow run retryability

Revision ID: 20260512_0025
Revises: 20260510_0024
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260512_0025"
down_revision = "20260510_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("is_retryable", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "is_retryable")
