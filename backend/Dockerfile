FROM python:3.12-slim AS runtime

# 中国网络环境可在 .env 设 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-wqy-zenhei \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY README.md /README.md
COPY backend/pyproject.toml backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/src ./src

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /app/storage/logs

EXPOSE 29280

CMD ["uvicorn", "productflow_backend.main:app", "--host", "0.0.0.0", "--port", "29280"]
