"""add standalone user accounts foundation (batch B, additive only)

Revision ID: 20260916_0036
Revises: 20260915_0035

纯新增迁移（批次 B 第一批）：
- 新表 users / user_invites / user_sessions；
- 业务表 products / agent_sessions / image_sessions / asset_library / copy_reports
  增加可空 owner_id 列（FK → users.id, ondelete SET NULL）并建索引。
不加 NOT NULL、不回填任何数据；downgrade 仅删除本迁移新增的对象，无数据损失
（业务表 owner_id 列里若已写入的值会随列一起删除，属预期回滚路径）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260916_0036"
down_revision = "20260915_0035"
branch_labels = None
depends_on = None

_OWNER_TABLES = (
    "products",
    "agent_sessions",
    "image_sessions",
    "asset_library",
    "copy_reports",
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_users_username", "users", ["username"], unique=True)

    op.create_table(
        "user_invites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.String(length=36), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_user_invites_token_hash", "user_invites", ["token_hash"], unique=True)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_sessions_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("uq_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)

    # 业务表归属列：SQLite 不支持 ALTER ADD CONSTRAINT（带命名的 FK 会触发独立
    # ADD CONSTRAINT），必须走 batch 模式（copy-and-move）。Postgres 侧 batch 退化为
    # 普通 ADD COLUMN，行为一致。
    for table in _OWNER_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "owner_id",
                    sa.String(length=36),
                    sa.ForeignKey("users.id", ondelete="SET NULL", name=f"fk_{table}_owner_id"),
                    nullable=True,
                )
            )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade() -> None:
    for table in _OWNER_TABLES:
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("owner_id")
    op.drop_index("uq_user_sessions_token_hash", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("uq_user_invites_token_hash", table_name="user_invites")
    op.drop_table("user_invites")
    op.drop_index("uq_users_username", table_name="users")
    op.drop_table("users")
