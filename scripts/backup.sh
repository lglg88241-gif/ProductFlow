#!/usr/bin/env bash
# 备份 ProductFlow 的全部有状态数据：Postgres、media 存储卷、Redis 快照。
#
# 用法：
#   bash scripts/backup.sh                    # 备份到 ./backups/<时间戳>/
#   BACKUP_DIR=/mnt/nas/productflow bash scripts/backup.sh
#   DRY_RUN=1 bash scripts/backup.sh          # 只打印将执行的动作
#
# 产物：
#   <BACKUP_DIR>/<时间戳>/postgres.dump.gz    # pg_dump 自定义格式（pg_restore 可读）
#   <BACKUP_DIR>/<时间戳>/storage.tar.gz      # media 卷（素材/生成物/缩略图）
#   <BACKUP_DIR>/<时间戳>/redis.rdb.gz        # 队列快照（DB 是权威状态，此项锦上添花）
#   <BACKUP_DIR>/<时间戳>/manifest.txt        # 校验和 + 元信息，供 restore 校验
#
# 恢复到本机需用 scripts/restore.sh；只验证可恢复性用 scripts/backup_drill.sh。
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

dry_run="${DRY_RUN:-0}"
db_service="productflow-postgres"
redis_service="productflow-redis"
backend_service="productflow-backend"
db_user="productflow"
db_name="productflow"

read_dotenv_value() {
  local key="$1"
  [[ -f "$repo_root/.env" ]] || return 1
  awk -v key="$key" '
    /^[[:space:]]*(#|$)/ { next }
    {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      pattern = "^[[:space:]]*" key "[[:space:]]*="
      if (line ~ pattern) {
        sub(pattern "[[:space:]]*", "", line)
        sub(/[[:space:]]+#.*$/, "", line)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
        if ((substr(line, 1, 1) == "\"" && substr(line, length(line), 1) == "\"") ||
            (substr(line, 1, 1) == "'"'"'" && substr(line, length(line), 1) == "'"'"'")) {
          line = substr(line, 2, length(line) - 2)
        }
        print line
        exit
      }
    }
  ' "$repo_root/.env"
}

storage_host_path="${STORAGE_HOST_PATH:-$(read_dotenv_value STORAGE_HOST_PATH || true)}"
backup_root="${BACKUP_DIR:-$repo_root/backups}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
timestamp="$(date +%Y%m%d-%H%M%S)"
target_dir="$backup_root/$timestamp"

echo "[backup] repo_root=$repo_root"
echo "[backup] target=$target_dir"
echo "[backup] retention=${retention_days}d"

container_id_of() {
  docker compose ps -q "$1" 2>/dev/null | head -1
}

require_running() {
  local service="$1" cid
  cid="$(container_id_of "$service")"
  if [[ -z "$cid" ]]; then
    echo "[backup] 服务未运行，无法备份: $service" >&2
    echo "[backup] 先执行 docker compose up -d" >&2
    exit 1
  fi
}

if [[ "$dry_run" == "1" ]]; then
  cat <<EOF
[backup] dry-run：将执行以下步骤
  1. mkdir -p $target_dir
  2. docker compose exec -T $db_service pg_dump -U $db_user -Fc $db_name | gzip > postgres.dump.gz
  3. 打包 media 存储（$([[ -n "$storage_host_path" ]] && echo "宿主路径 $storage_host_path" || echo "named volume productflow-storage")）→ storage.tar.gz
  4. docker compose exec -T $redis_service redis-cli SAVE && 复制 dump.rdb → redis.rdb.gz
  5. 生成 manifest.txt（含 sha256 校验和）
  6. 清理超过 ${retention_days} 天的旧备份
[backup] dry-run 不会写入任何文件
EOF
  exit 0
fi

require_running "$db_service"
require_running "$redis_service"

mkdir -p "$target_dir"

echo "[backup] 1/4 Postgres 逻辑备份（pg_dump -Fc）"
docker compose exec -T "$db_service" pg_dump -U "$db_user" -Fc "$db_name" | gzip > "$target_dir/postgres.dump.gz"

echo "[backup] 2/4 media 存储打包"
if [[ -n "$storage_host_path" ]]; then
  if [[ ! -d "$storage_host_path" ]]; then
    echo "[backup] STORAGE_HOST_PATH 不存在: $storage_host_path" >&2
    exit 1
  fi
  tar -czf "$target_dir/storage.tar.gz" -C "$storage_host_path" .
else
  # named volume：借 backend 容器（与 worker 共享同一卷）打包 /app/storage 到 stdout，
  # 显式走临时文件再落位，避免容器告警污染归档。
  # MSYS_NO_PATHCONV=1：Git Bash 会把容器内绝对路径 /app/storage 改写成 Windows 路径。
  MSYS_NO_PATHCONV=1 docker compose exec -T "$backend_service" tar -czf - -C /app/storage . \
    > "$target_dir/storage.tar.gz.tmp"
  mv "$target_dir/storage.tar.gz.tmp" "$target_dir/storage.tar.gz"
fi

echo "[backup] 3/4 Redis 快照"
docker compose exec -T "$redis_service" redis-cli SAVE >/dev/null
redis_cid="$(container_id_of "$redis_service")"
docker cp "$redis_cid:/data/dump.rdb" "$target_dir/redis.rdb" >/dev/null
gzip -f "$target_dir/redis.rdb"

echo "[backup] 4/4 生成 manifest 并清理过期备份"
{
  echo "created_at=$(date -Iseconds)"
  echo "git_commit=$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "alembic_revision=$(docker compose exec -T "$backend_service" alembic current 2>/dev/null | tail -1 | awk '{print $1}' || echo unknown)"
  echo "postgres_db=$db_name"
  echo "storage_source=$([[ -n "$storage_host_path" ]] && echo "host:$storage_host_path" || echo 'volume:productflow-storage')"
  echo "--- sha256 ---"
  (cd "$target_dir" && sha256sum postgres.dump.gz storage.tar.gz redis.rdb.gz)
} > "$target_dir/manifest.txt"

size="$(du -sh "$target_dir" | cut -f1)"
echo "[backup] 完成：$target_dir（$size）"
cat "$target_dir/manifest.txt" | sed 's/^/[backup]   /'

if [[ "$retention_days" -gt 0 ]] && [[ -d "$backup_root" ]]; then
  pruned=0
  while IFS= read -r old_dir; do
    [[ -n "$old_dir" ]] || continue
    echo "[backup] 清理过期备份: $(basename "$old_dir")"
    rm -rf "$old_dir"
    pruned=$((pruned + 1))
  done < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$retention_days" 2>/dev/null)
  echo "[backup] 清理完成，共删除 $pruned 个过期备份"
fi

echo "[backup] 提示：定期用 bash scripts/backup_drill.sh <备份目录> 验证可恢复性"
