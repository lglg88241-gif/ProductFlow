# 审计 O2：多阶段构建 + uv 锁定安装（uv sync --frozen）+ 非 root 运行。
# 依赖清单/锁文件先于源码拷贝：pyproject/uv.lock 不变时依赖层直接走缓存。
# 中国网络环境可在 .env 设 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ARG PIP_INDEX_URL=https://pypi.org/simple

FROM python:3.12-slim AS builder

ARG PIP_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    # uv 不读 PIP_INDEX_URL，用 UV_DEFAULT_INDEX 保持镜像源一致
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv 固定版本：保证锁文件解析与安装行为可复现
RUN pip install --no-cache-dir "uv==0.12.13"

WORKDIR /app

# 先只拷依赖清单 + 锁文件（依赖缓存层）
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷源码并安装项目本体（editable 指向 /app/src，运行阶段保持同路径）
COPY backend/src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 运行必需系统包沿用旧镜像（中文字体 + rapidocr/onnxruntime 动态库）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-wqy-zenhei \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 运行阶段只带 .venv 与源码/迁移，不带构建工具
COPY --from=builder /app/.venv /app/.venv
COPY backend/src ./src
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./

ENV PATH="/app/.venv/bin:${PATH}"

# 非 root：compose 挂载的命名卷首次初始化会继承 /app/storage 的属主
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/storage/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 29280

# CMD 与旧镜像完全一致；compose 的 worker service 用 command 覆盖为 dramatiq，
# backend service 覆盖为 alembic upgrade head && uvicorn（alembic 在 /app/.venv/bin 内）。
CMD ["uvicorn", "productflow_backend.main:app", "--host", "0.0.0.0", "--port", "29280"]
