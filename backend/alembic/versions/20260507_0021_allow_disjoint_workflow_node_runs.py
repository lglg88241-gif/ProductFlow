"""allow disjoint workflow node runs

Revision ID: 20260507_0021
Revises: 20260507_0020
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20260507_0021"
down_revision = "20260507_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    workflow_run_index_names = {index["name"] for index in inspect(bind).get_indexes("workflow_runs")}
    if "uq_workflow_runs_one_running_per_workflow" in workflow_run_index_names:
        op.drop_index("uq_workflow_runs_one_running_per_workflow", table_name="workflow_runs")
    op.execute(
        """
        UPDATE workflow_node_runs
        SET status = 'failed',
            finished_at = CURRENT_TIMESTAMP,
            failure_reason = COALESCE(failure_reason, '重复节点运行已关闭')
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY node_id
                        ORDER BY started_at DESC, id DESC
                    ) AS duplicate_rank
                FROM workflow_node_runs
                WHERE status IN ('queued', 'running')
            ) ranked_active_node_runs
            WHERE duplicate_rank > 1
        )
        """
    )
    op.create_index(
        "uq_workflow_node_runs_one_active_per_node",
        "workflow_node_runs",
        ["node_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    node_run_index_names = {index["name"] for index in inspect(bind).get_indexes("workflow_node_runs")}
    if "uq_workflow_node_runs_one_active_per_node" in node_run_index_names:
        op.drop_index("uq_workflow_node_runs_one_active_per_node", table_name="workflow_node_runs")
    duplicate_run_ids = """
        SELECT id
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY workflow_id
                    ORDER BY started_at DESC, id DESC
                ) AS duplicate_rank
            FROM workflow_runs
            WHERE status = 'running'
        ) ranked_running_runs
        WHERE duplicate_rank > 1
    """
    op.execute(
        f"""
        UPDATE workflow_nodes
        SET status = CASE WHEN status = 'running' THEN 'failed' ELSE 'idle' END,
            failure_reason = CASE WHEN status = 'running' THEN COALESCE(failure_reason, '重复运行已关闭') ELSE NULL END,
            last_run_at = CASE WHEN status = 'running' THEN CURRENT_TIMESTAMP ELSE last_run_at END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT node_id
            FROM workflow_node_runs
            WHERE workflow_run_id IN ({duplicate_run_ids})
              AND status IN ('queued', 'running')
        )
        """
    )
    op.execute(
        f"""
        UPDATE workflow_node_runs
        SET status = 'failed',
            finished_at = CURRENT_TIMESTAMP,
            failure_reason = COALESCE(failure_reason, '重复运行已关闭')
        WHERE workflow_run_id IN ({duplicate_run_ids})
          AND status IN ('queued', 'running')
        """
    )
    op.execute(
        f"""
        UPDATE workflow_runs
        SET status = 'failed',
            finished_at = CURRENT_TIMESTAMP,
            failure_reason = COALESCE(failure_reason, '重复运行已关闭')
        WHERE id IN ({duplicate_run_ids})
        """
    )
    op.create_index(
        "uq_workflow_runs_one_running_per_workflow",
        "workflow_runs",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )
