# syntax=docker/dockerfile:1.7

# ---- SQLite 3.53.0 builder ---------------------------------------------------
# E33 (Perplexity Max Scout-1, 2026-04-27): SQLite 3.51.3 / 3.53.0 修了 WAL-reset
# database corruption bug。Debian bookworm/trixie 系統 sqlite (3.46.1) 不夠新，
# 必須從 source 編譯 ≥ 3.51.3 才能避開 production data corruption 風險。
# 選 3.53.0 (2026-04-09 release) = 最新 stable + 含 WAL-reset fix。
FROM debian:bookworm-slim AS sqlite-builder

ARG SQLITE_VERSION=3530000
ARG SQLITE_YEAR=2026

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN wget -q "https://www.sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && tar xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && ./configure \
        --prefix=/opt/sqlite \
        --enable-fts5 \
        --enable-load-extension \
        CFLAGS="-O2 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_LOAD_EXTENSION" \
    && make -j"$(nproc)" \
    && make install \
    && /opt/sqlite/bin/sqlite3 --version


# ---- Python deps builder -----------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    LD_LIBRARY_PATH=/opt/sqlite/lib:${LD_LIBRARY_PATH}

# Inject upgraded SQLite — Python sqlite3 module dynamic-loads libsqlite3.so
# from LD_LIBRARY_PATH first, shadowing system 3.46.1.
COPY --from=sqlite-builder /opt/sqlite /opt/sqlite
RUN echo "/opt/sqlite/lib" > /etc/ld.so.conf.d/sqlite-upgrade.conf && ldconfig

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

# Use /app as workdir so venv shebangs point to /app/.venv/bin/python,
# which is where runtime stage will mount the venv.
WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ollama 2>/dev/null || \
    uv sync --no-dev --extra ollama

# Build-time fail-fast: assert SQLite ≥ 3.51.3 + sqlite-vec compatibility (E33)
RUN /app/.venv/bin/python -c "\
import sqlite3, sqlite_vec; \
ver = sqlite3.sqlite_version_info; \
assert ver >= (3, 51, 3), f'SQLite {sqlite3.sqlite_version} < 3.51.3 (WAL-reset corruption bug, E33)'; \
c = sqlite3.connect(':memory:'); \
c.enable_load_extension(True); \
sqlite_vec.load(c); \
v = c.execute('SELECT vec_version()').fetchone()[0]; \
print(f'SQLite={sqlite3.sqlite_version}, vec0={v}')"


# ---- Runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LD_LIBRARY_PATH=/opt/sqlite/lib:${LD_LIBRARY_PATH} \
    MH_HOST=0.0.0.0 \
    MH_PORT=9000 \
    MH_DATABASE_PATH=/data/memory-hall.sqlite3 \
    MH_VECTOR_DATABASE_PATH=/data/memory-hall-vectors.sqlite3

# Inject upgraded SQLite to runtime stage too
COPY --from=sqlite-builder /opt/sqlite /opt/sqlite
RUN echo "/opt/sqlite/lib" > /etc/ld.so.conf.d/sqlite-upgrade.conf && ldconfig

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
