"""add structured copy payload

Revision ID: 20260509_0023
Revises: 20260508_0022
Create Date: 2026-05-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260509_0023"
down_revision = "20260508_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("copy_sets", sa.Column("structured_payload", sa.JSON(), nullable=True))
    op.add_column("copy_sets", sa.Column("model_structured_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("copy_sets", "model_structured_payload")
    op.drop_column("copy_sets", "structured_payload")
