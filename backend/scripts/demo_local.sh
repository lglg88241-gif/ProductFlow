#!/usr/bin/env bash
# 一键本地自动化演示：sqlite + mock 供应商（自动迁移 → 起服 → 跑全功能演示）。
# 用法：bash scripts/demo_local.sh
# 需要真实 Agent 对话时，先配置 OpenAI 兼容文本供应商并 export 相应环境变量。
set -euo pipefail
cd "$(dirname "$0")/.."

export ADMIN_ACCESS_KEY="${ADMIN_ACCESS_KEY:-demo-admin-key-123}"
export SETTINGS_ACCESS_TOKEN="${SETTINGS_ACCESS_TOKEN:-demo-settings-token-456}"
export SESSION_SECRET="${SESSION_SECRET:-demo-session-secret-0987654321}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./demo.db}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6399/0}"
export STORAGE_ROOT="${STORAGE_ROOT:-./storage-demo}"
export LOG_DIR="${LOG_DIR:-./storage-demo/logs}"
export TEXT_PROVIDER_KIND="${TEXT_PROVIDER_KIND:-mock}"
export IMAGE_PROVIDER_KIND="${IMAGE_PROVIDER_KIND:-mock}"
PORT="${DEMO_PORT:-29290}"

echo "▶ 1/3 数据库迁移（alembic upgrade head）"
uv run alembic upgrade head

echo "▶ 2/3 启动后端（127.0.0.1:${PORT}）"
uv run uvicorn productflow_backend.main:app --host 127.0.0.1 --port "${PORT}" &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/healthz" > /dev/null; then
    break
  fi
  sleep 1
done

echo "▶ 3/3 执行全功能演示"
uv run python scripts/demo_flow.py \
  --base-url "http://127.0.0.1:${PORT}" \
  --admin-key "${ADMIN_ACCESS_KEY}" \
  --settings-token "${SETTINGS_ACCESS_TOKEN}" \
  --generation-timeout "${GENERATION_TIMEOUT:-60}" \
  --output demo-report.md
