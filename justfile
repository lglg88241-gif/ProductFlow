set dotenv-load := true

backend-install:
    uv sync --directory backend --extra dev

backend-run:
    bash scripts/with_dev_env.sh bash -lc 'uv run --directory backend uvicorn productflow_backend.main:app --reload --host 0.0.0.0 --port "${APP_PORT:-29282}"'

backend-run-prod:
    uv run --directory backend uvicorn productflow_backend.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-29280}

backend-worker:
    bash scripts/with_dev_env.sh uv run --directory backend dramatiq --processes 2 --threads 4 productflow_backend.workers

backend-migrate:
    bash scripts/with_dev_env.sh uv run --directory backend alembic upgrade head

backend-migrate-prod:
    uv run --directory backend alembic upgrade head

backend-worker-prod:
    uv run --directory backend dramatiq --processes 2 --threads 4 productflow_backend.workers

backend-test:
    uv run --directory backend pytest

backend-lint:
    uv run --directory backend ruff check .

web-install:
    pnpm --dir web install

web-lint:
    pnpm --dir web lint

web-test:
    pnpm --dir web test:run

# 本地一键复现 CI 门禁（CI 的 backend 测试额外带 --cov --cov-fail-under=88）
ci:
    just backend-lint
    just backend-test
    just web-lint
    just web-test
    just web-build

web-dev:
    bash scripts/with_dev_env.sh bash -lc 'web_port="${WEB_PORT:-29283}"; api_target="${VITE_DEV_PROXY_TARGET:-http://127.0.0.1:${APP_PORT:-29282}}"; VITE_API_BASE_URL= VITE_DEV_PROXY_TARGET="$api_target" pnpm --dir web dev -- --host 0.0.0.0 --port "$web_port" --strictPort'

web-preview-prod:
    pnpm --dir web preview -- --host 0.0.0.0 --port ${WEB_PORT:-29281} --strictPort

web-build:
    pnpm --dir web build
release:
    bash scripts/release.sh

release-dry-run:
    DRY_RUN=1 bash scripts/release.sh

# Agent 质量评测（golden set）：真实模型跑一批小白需求，给路由/澄清/话术打分
agent-eval:
    uv run --directory backend python scripts/agent_eval.py --base-url http://127.0.0.1:${WEB_PORT:-29281} --admin-key ${ADMIN_ACCESS_KEY}

# 含生图场景的完整评测（消耗生图额度）
agent-eval-full:
    uv run --directory backend python scripts/agent_eval.py --base-url http://127.0.0.1:${WEB_PORT:-29281} --admin-key ${ADMIN_ACCESS_KEY} --include-costly

# 备份 Postgres + media 存储 + Redis 快照到 ./backups/<时间戳>/
backup:
    bash scripts/backup.sh

# 恢复演练：把最新备份恢复到一次性临时容器验证可恢复性（不动生产数据）
backup-drill:
    bash scripts/backup_drill.sh

# 从备份恢复（破坏性：覆盖当前数据库与存储，需 RESTORE_CONFIRM=yes 或交互确认）
restore dir:
    bash scripts/restore.sh {{dir}}

# 对运行中的实例执行全功能演示（compose 栈或已启动的后端）
demo:
    uv run --directory backend python scripts/demo_flow.py

# 一键本地演示：sqlite + mock 供应商（自动迁移 → 起服 → 全功能演示）
demo-local:
    bash backend/scripts/demo_local.sh
