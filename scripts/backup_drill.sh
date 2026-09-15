#!/usr/bin/env bash
# 恢复演练：把备份恢复到一个**一次性临时容器**里验证可恢复性，绝不触碰生产数据。
#
# 这是"备份有没有用"的唯一可信证据——定期跑一次（建议每月或每次改完迁移后）。
#
# 用法：
#   bash scripts/backup_drill.sh                          # 用 backups/ 下最新一份备份
#   bash scripts/backup_drill.sh backups/20260915-101500  # 指定备份目录
#
# 动作：
#   1. 起一个临时 Postgres 容器（独立卷、随机端口），不接入生产网络
#   2. 把 postgres.dump.gz 恢复进去
#   3. 校验：表数量、关键表行数、alembic 版本与备份 manifest 是否一致
#   4. 校验 storage.tar.gz 可解包且文件数 > 0
#   5. 无论成败都清理临时容器与临时卷
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

backup_root="${BACKUP_DIR:-$repo_root/backups}"
backup_dir="${1:-}"

if [[ -z "$backup_dir" ]]; then
  # 取最新一份备份（按目录名时间戳排序）
  backup_dir="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)"
  if [[ -z "$backup_dir" ]]; then
    echo "[drill] $backup_root 下没有备份，先执行 bash scripts/backup.sh" >&2
    exit 1
  fi
  echo "[drill] 未指定备份目录，使用最新一份: $backup_dir"
fi

[[ -d "$backup_dir" ]] || { echo "[drill] 备份目录不存在: $backup_dir" >&2; exit 1; }
[[ -f "$backup_dir/postgres.dump.gz" ]] || { echo "[drill] 缺少 postgres.dump.gz" >&2; exit 1; }

db_user="productflow"
db_name="productflow"
container="productflow-drill-$$"
volume="productflow-drill-data-$$"
drill_password="drill-only-not-a-secret"

cleanup() {
  echo "[drill] 清理临时容器与卷"
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[drill] 1/5 启动临时 Postgres（$container）"
docker volume create "$volume" >/dev/null
docker run -d --name "$container" \
  -e POSTGRES_USER="$db_user" \
  -e POSTGRES_PASSWORD="$drill_password" \
  -e POSTGRES_DB="$db_name" \
  -v "$volume:/var/lib/postgresql/data" \
  postgres:16 >/dev/null

for _ in $(seq 1 40); do
  if docker exec "$container" pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "[drill] 2/5 恢复备份"
# 演练用库是干净的空库，pg_restore 到默认 schema 即可（生产恢复脚本才会先 DROP SCHEMA）
gunzip -c "$backup_dir/postgres.dump.gz" | docker exec -i "$container" \
  pg_restore -U "$db_user" -d "$db_name" --no-owner --no-privileges --exit-on-error

echo "[drill] 3/5 校验数据库内容"
table_count="$(docker exec "$container" psql -U "$db_user" -d "$db_name" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
revision="$(docker exec "$container" psql -U "$db_user" -d "$db_name" -tAc \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || echo "（无 alembic_version 表）")"

echo "[drill]   表数量: $table_count"
echo "[drill]   迁移版本: $revision"

# 关键表行数巡检（表存在才统计，避免因版本差异误报）
for table in products image_sessions agent_sessions agent_messages asset_library; do
  exists="$(docker exec "$container" psql -U "$db_user" -d "$db_name" -tAc \
    "SELECT to_regclass('public.$table') IS NOT NULL")"
  if [[ "$exists" == "t" ]]; then
    rows="$(docker exec "$container" psql -U "$db_user" -d "$db_name" -tAc "SELECT count(*) FROM $table")"
    echo "[drill]   $table: $rows 行"
  else
    echo "[drill]   $table: 备份中不存在该表"
  fi
done

expected_revision="$(grep '^alembic_revision=' "$backup_dir/manifest.txt" 2>/dev/null | cut -d= -f2 || true)"
if [[ -n "$expected_revision" && "$expected_revision" != "unknown" && "$revision" != "$expected_revision" ]]; then
  echo "[drill] 失败：迁移版本与 manifest 不一致（备份=$expected_revision 恢复后=$revision）" >&2
  exit 1
fi

if [[ "$table_count" -lt 10 ]]; then
  echo "[drill] 失败：恢复后表数量异常偏少（$table_count），备份可能不完整" >&2
  exit 1
fi

echo "[drill] 4/5 校验 media 存储归档"
if [[ -f "$backup_dir/storage.tar.gz" ]]; then
  file_count="$(tar -tzf "$backup_dir/storage.tar.gz" | grep -cv '/$' || true)"
  echo "[drill]   归档内文件数: $file_count"
  if [[ "$file_count" -eq 0 ]]; then
    echo "[drill] 警告：存储归档为空（如果确实还没有素材则正常）" >&2
  fi
else
  echo "[drill] 未找到 storage.tar.gz，跳过" >&2
fi

echo "[drill] 5/5 演练通过：备份可恢复（数据库 $table_count 张表，迁移 $revision）"
