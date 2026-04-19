# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

# Use /app as workdir so venv shebangs point to /app/.venv/bin/python,
# which is where runtime stage will mount the venv.
WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ollama 2>/dev/null || \
    uv sync --no-dev --extra ollama

RUN uv pip install --python /app/.venv/bin/python --reinstall --no-binary sqlite-vec 'sqlite-vec==0.1.6'


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MH_HOST=0.0.0.0 \
    MH_PORT=9000 \
    MH_DATABASE_PATH=/data/memory-hall.sqlite3 \
    MH_VECTOR_DATABASE_PATH=/data/memory-hall-vectors.sqlite3

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --shell /usr/sbin/nologin memhall \
    && mkdir -p /data \
    && chown memhall:memhall /data

WORKDIR /app

COPY --from=builder --chown=memhall:memhall /app/.venv /app/.venv
COPY --chown=memhall:memhall src /app/src
COPY --chown=memhall:memhall pyproject.toml README.md /app/

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src"

USER memhall

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${MH_PORT}/v1/health || exit 1

ENTRYPOINT ["memory-hall"]
CMD ["serve"]
