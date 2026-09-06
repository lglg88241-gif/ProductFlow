"""add workflow run progress metadata

Revision ID: 20260513_0026
Revises: 20260512_0025
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260513_0026"
down_revision = "20260512_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("progress_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "progress_metadata")
