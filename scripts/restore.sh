#!/usr/bin/env bash
# 从备份恢复 ProductFlow 的 Postgres 与 media 存储。
#
# ⚠️ 这是破坏性操作：会覆盖当前数据库与存储卷内容。脚本要求显式确认
#   （RESTORE_CONFIRM=yes）或 --dry-run。
#
# 用法：
#   bash scripts/restore.sh backups/20260915-101500            # 交互确认后恢复
#   bash scripts/restore.sh backups/20260915-101500 --dry-run  # 只打印将执行的动作
#   RESTORE_CONFIRM=yes bash scripts/restore.sh <dir>          # 跳过交互（自动化用）
#   RESTORE_TARGET=storage bash scripts/restore.sh <dir>       # 只恢复存储
#   RESTORE_TARGET=db bash scripts/restore.sh <dir>            # 只恢复数据库
#
# 恢复顺序说明：先停 backend/worker（避免恢复期间的写入与半截任务），
# 再恢复 DB 与存储，最后重启并等待健康检查通过。
#
# 只验证备份可用性、不想动生产时用 scripts/backup_drill.sh。
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

backup_dir=""
dry_run=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h|--help) usage ;;
    -*) echo "[restore] 未知参数: $arg" >&2; usage ;;
    *) backup_dir="$arg" ;;
  esac
done

[[ -n "$backup_dir" ]] || usage
[[ -d "$backup_dir" ]] || { echo "[restore] 备份目录不存在: $backup_dir" >&2; exit 1; }

restore_target="${RESTORE_TARGET:-all}"
db_service="productflow-postgres"
backend_service="productflow-backend"
worker_service="productflow-worker"
web_service="productflow-web"
db_user="productflow"
db_name="productflow"
storage_volume="productflow-storage"

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

# 1) 校验备份完整性（manifest 存在则逐文件比对 sha256）
echo "[restore] 校验备份：$backup_dir"
if [[ -f "$backup_dir/manifest.txt" ]]; then
  sed 's/^/[restore]   /' "$backup_dir/manifest.txt"
  if grep -q '^--- sha256 ---$' "$backup_dir/manifest.txt"; then
    echo "[restore] 校验 sha256"
    (cd "$backup_dir" && sed -n '/^--- sha256 ---$/,$p' manifest.txt | tail -n +2 | sha256sum -c -) \
      || { echo "[restore] 备份文件校验失败，已中止" >&2; exit 1; }
  fi
else
  echo "[restore] 警告：缺少 manifest.txt，跳过校验和验证" >&2
fi

do_db=0
do_storage=0
case "$restore_target" in
  all) do_db=1; do_storage=1 ;;
  db) do_db=1 ;;
  storage) do_storage=1 ;;
  *) echo "[restore] RESTORE_TARGET 只能是 all/db/storage，收到: $restore_target" >&2; exit 1 ;;
esac

if [[ "$do_db" == "1" ]] && [[ ! -f "$backup_dir/postgres.dump.gz" ]]; then
  echo "[restore] 缺少 postgres.dump.gz" >&2; exit 1
fi
if [[ "$do_storage" == "1" ]] && [[ ! -f "$backup_dir/storage.tar.gz" ]]; then
  echo "[restore] 缺少 storage.tar.gz" >&2; exit 1
fi

if [[ "$dry_run" == "1" ]]; then
  cat <<EOF
[restore] dry-run：将执行以下步骤（target=$restore_target）
  1. docker compose stop $backend_service $worker_service $web_service
$( [[ "$do_db" == "1" ]] && echo "  2. 重建数据库 $db_name（DROP SCHEMA public CASCADE）并 pg_restore 灌入 postgres.dump.gz" )
$( [[ "$do_storage" == "1" ]] && echo "  3. 清空并解包 media 存储（$([[ -n "$storage_host_path" ]] && echo "宿主路径 $storage_host_path" || echo "named volume $storage_volume")）" )
  4. docker compose up -d && 等待 backend/web 健康检查通过
  5. 打印恢复后的 /healthz 与 alembic current
[restore] dry-run 不会修改任何数据
EOF
  exit 0
fi

if [[ "${RESTORE_CONFIRM:-no}" != "yes" ]]; then
  echo "[restore] 即将覆盖当前数据库与存储，且不可撤销。"
  if [[ ! -t 0 ]]; then
    echo "[restore] 非交互环境请显式设置 RESTORE_CONFIRM=yes" >&2
    exit 1
  fi
  read -r -p "[restore] 输入 yes 继续: " answer
  [[ "$answer" == "yes" ]] || { echo "[restore] 已取消"; exit 1; }
fi

echo "[restore] 1/5 停止写入方（backend/worker/web）"
docker compose stop "$backend_service" "$worker_service" "$web_service" >/dev/null

if [[ "$do_db" == "1" ]]; then
  echo "[restore] 2/5 重建并恢复数据库"
  docker compose up -d "$db_service" >/dev/null
  for _ in $(seq 1 30); do
    if docker compose exec -T "$db_service" pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  # 清空现有 schema 再灌入，避免残留旧表与备份冲突
  docker compose exec -T "$db_service" psql -U "$db_user" -d "$db_name" -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
  gunzip -c "$backup_dir/postgres.dump.gz" | docker compose exec -T "$db_service" \
    pg_restore -U "$db_user" -d "$db_name" --no-owner --no-privileges --exit-on-error
  echo "[restore] 数据库恢复完成"
fi

if [[ "$do_storage" == "1" ]]; then
  echo "[restore] 3/5 恢复 media 存储"
  if [[ -n "$storage_host_path" ]]; then
    mkdir -p "$storage_host_path"
    find "$storage_host_path" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    tar -xzf "$backup_dir/storage.tar.gz" -C "$storage_host_path"
  else
    docker volume create "$storage_volume" >/dev/null
    docker run --rm -v "$storage_volume:/data" -v "$backup_dir:/backup:ro" alpine:3 \
      sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf /backup/storage.tar.gz -C /data' \
      >/dev/null
  fi
  echo "[restore] 存储恢复完成"
fi

echo "[restore] 4/5 重启服务"
docker compose up -d >/dev/null

echo "[restore] 5/5 等待健康检查"
backend_port="${APP_HOST_PORT:-29280}"
web_port="${WEB_PORT:-29281}"
for label_url in "backend|http://127.0.0.1:${backend_port}/healthz" "web|http://127.0.0.1:${web_port}/healthz"; do
  label="${label_url%%|*}"
  url="${label_url#*|}"
  ok=0
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [[ "$ok" == "1" ]]; then
    echo "[restore] ${label} 健康: $url"
  else
    echo "[restore] ${label} 未能通过健康检查: $url（请查 docker compose logs）" >&2
  fi
done

echo "[restore] 当前迁移版本: $(docker compose exec -T "$backend_service" alembic current 2>/dev/null | tail -1)"
echo "[restore] 完成。建议核对：商品/会话列表是否完整、素材图片能否下载预览。"
