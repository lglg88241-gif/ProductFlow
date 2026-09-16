"""批次 B · 现有数据归属回填（owner_id → 初始管理员）。

审计要求：回填前必须有已演练通过的备份；回填必须幂等；执行前后逐表对账；
内置模板素材保持全体可读（owner_id 维持 NULL），不得归属给任何个人。

用法（在 backend 容器内或本机 venv 运行）：
    python -m scripts.backfill_owner --admin-username lgs            # 试运行（只打印计划）
    python -m scripts.backfill_owner --admin-username lgs --apply    # 实际执行

执行前提：
    1. 已完成 `bash scripts/backup.sh` 且 `backup_drill.sh` 通过；
    2. `users` 表里存在 --admin-username 对应的 role=admin 账号。
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select, text

from productflow_backend.application.user_accounts import get_user_by_username
from productflow_backend.infrastructure.db.models import UserAccount
from productflow_backend.infrastructure.db.session import get_session_factory

# (表名, 内置豁免条件——asset_library 的 builtin 模板保持全体可读)
BACKFILL_TABLES: tuple[tuple[str, str | None], ...] = (
    ("products", None),
    ("agent_sessions", None),
    ("image_sessions", None),
    ("copy_reports", None),
    ("asset_library", "source != 'builtin'"),  # 内置模板全体可读，不归属
)


def _table_counts(session, table: str, exempt_clause: str | None) -> dict[str, int]:
    where_exempt = f" WHERE {exempt_clause}" if exempt_clause else ""
    total = session.execute(text(f"SELECT count(*) FROM {table}{where_exempt}")).scalar_one()
    owned = session.execute(
        text(f"SELECT count(*) FROM {table}{where_exempt}{' AND' if exempt_clause else ' WHERE'} owner_id IS NOT NULL")
    ).scalar_one()
    return {"total": int(total), "owned": int(owned), "pending": int(total) - int(owned)}


def main() -> int:
    parser = argparse.ArgumentParser(description="回填现有数据的 owner_id")
    parser.add_argument("--admin-username", required=True)
    parser.add_argument("--apply", action="store_true", help="实际执行（默认只打印计划）")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        admin = get_user_by_username(session, args.admin_username)
        if admin is None:
            print(f"[backfill] 失败：用户不存在: {args.admin_username}（先用 initialization create-admin 创建）")
            return 1
        if admin.role != "admin":
            print(f"[backfill] 失败：{args.admin_username} 不是 admin 角色，拒绝把数据归属给它")
            return 1

        print(f"[backfill] 目标管理员: {admin.username}（{admin.id}）")
        print(f"[backfill] 模式: {'APPLY' if args.apply else 'DRY-RUN（加 --apply 实际执行）'}\n")

        before: dict[str, dict[str, int]] = {}
        plan_ok = True
        for table, exempt in BACKFILL_TABLES:
            counts = _table_counts(session, table, exempt)
            before[table] = counts
            print(
                f"  {table:18s} 总 {counts['total']:5d}  已归属 {counts['owned']:5d}"
                f"  待回填 {counts['pending']:5d}"
                f"{'  （builtin 豁免）' if exempt else ''}"
            )
        print()

        if not args.apply:
            print("[backfill] dry-run 结束，未修改任何数据。")
            return 0

        admin_id = str(admin.id)
        for table, exempt in BACKFILL_TABLES:
            where = "owner_id IS NULL"
            if exempt:
                where += f" AND {exempt}"
            result = session.execute(
                text(f"UPDATE {table} SET owner_id = :admin WHERE {where}"),
                {"admin": admin_id},
            )
            print(f"  {table:18s} 回填 {result.rowcount} 行")
        session.commit()

        # 执行后对账：行数不变 + 应归属的行不再有 NULL
        problems: list[str] = []
        for table, exempt in BACKFILL_TABLES:
            after = _table_counts(session, table, exempt)
            if after["total"] != before[table]["total"]:
                problems.append(f"{table}: 行数变化 {before[table]['total']} -> {after['total']}")
            if after["pending"] != 0:
                problems.append(f"{table}: 仍有 {after['pending']} 行未归属")
            print(
                f"  {table:18s} 总 {after['total']:5d}（前 {before[table]['total']:5d}）  "
                f"已归属 {after['owned']:5d}  待回填 {after['pending']:5d}"
            )

        # 内置模板必须保持 NULL（全体可读），误归属视为失败
        builtin_owned = session.execute(
            text("SELECT count(*) FROM asset_library WHERE source = 'builtin' AND owner_id IS NOT NULL")
        ).scalar_one()
        if int(builtin_owned) != 0:
            problems.append(f"内置模板被误归属 {builtin_owned} 行（应保持 NULL = 全体可读）")

        if problems:
            print("\n[backfill] 失败：对账发现问题（回滚本次事务前请先核对备份）:")
            for problem in problems:
                print(f"  - {problem}")
            return 1

        print("\n[backfill] 完成：回填与对账全部通过。")
        print("[backfill] 提醒：归属过滤尚未启用——启用是下一批（需双账号交叉验证）。")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
