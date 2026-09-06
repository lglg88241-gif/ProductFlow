"""add provider profiles and bindings

Revision ID: 20260513_0027
Revises: 20260513_0026
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260513_0027"
down_revision = "20260513_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("default_models_json", sa.JSON(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_profiles_archived_at", "provider_profiles", ["archived_at"], unique=False)
    op.create_index("ix_provider_profiles_enabled", "provider_profiles", ["enabled"], unique=False)

    op.create_table(
        "provider_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("provider_kind", sa.String(length=40), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=36), nullable=True),
        sa.Column("model_settings_json", sa.JSON(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_profile_id"], ["provider_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_provider_bindings_purpose", "provider_bindings", ["purpose"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_provider_bindings_purpose", table_name="provider_bindings")
    op.drop_table("provider_bindings")
    op.drop_index("ix_provider_profiles_enabled", table_name="provider_profiles")
    op.drop_index("ix_provider_profiles_archived_at", table_name="provider_profiles")
    op.drop_table("provider_profiles")
