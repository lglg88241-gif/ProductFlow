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

# 对运行中的实例执行全功能演示（compose 栈或已启动的后端）
demo:
    uv run --directory backend python scripts/demo_flow.py

# 一键本地演示：sqlite + mock 供应商（自动迁移 → 起服 → 全功能演示）
demo-local:
    bash backend/scripts/demo_local.sh
