"""repair provider bindings primary key

Revision ID: 20260513_0028
Revises: 20260513_0027
Create Date: 2026-05-13
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "20260513_0028"
down_revision = "20260513_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "provider_bindings" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("provider_bindings")}
    if "id" in columns:
        return

    op.add_column("provider_bindings", sa.Column("id", sa.String(length=36), nullable=True))

    rows = bind.execute(sa.text("select purpose from provider_bindings where id is null")).mappings().all()
    for row in rows:
        bind.execute(
            sa.text("update provider_bindings set id = :id where purpose = :purpose"),
            {"id": str(uuid.uuid4()), "purpose": row["purpose"]},
        )

    op.drop_constraint("provider_bindings_pkey", "provider_bindings", type_="primary")
    op.alter_column("provider_bindings", "id", existing_type=sa.String(length=36), nullable=False)
    op.create_primary_key("provider_bindings_pkey", "provider_bindings", ["id"])
    op.create_index("uq_provider_bindings_purpose", "provider_bindings", ["purpose"], unique=True)


def downgrade() -> None:
    pass
