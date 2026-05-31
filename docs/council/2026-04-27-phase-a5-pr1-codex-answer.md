# 2026-04-27 Phase A.5 PR1 Codex Summary

Source briefing: [AGENT: Claude]

Note:
本檔未追加成第 5 個 commit，刻意保留「4 個 patch commit」的 review 邊界。

## Patch A

- Commit: `c17b8a0` `分頁化 admin reindex 掃描流程`
- Diff stat:
  - `src/memory_hall/server/app.py` `97 insertions, 24 deletions`
  - `src/memory_hall/storage/interface.py` `8 insertions`
  - `src/memory_hall/storage/sqlite_store.py` `34 insertions`
  - `tests/test_sync_status.py` `117 insertions, 1 deletion`
- Main changes:
  - `src/memory_hall/server/app.py:517` reworked `_handle_reindex()` into a cursor-based loop with fixed `limit=200`, batch progress logs, and `CancelledError` passthrough after already-finished batches.
  - `src/memory_hall/storage/sqlite_store.py:210` added `sync_status` filtering to `list_entries()`.
  - `src/memory_hall/storage/sqlite_store.py:251` added `count_entries()` so the worker can pre-compute total batch count.
  - `tests/test_sync_status.py:106` proves admin full reindex over `205` failed rows never calls `limit=None`.
  - `tests/test_sync_status.py:144` proves `pending_only=True` still paginates only `sync_status='pending'`.

## Patch B

- Commit: `31ddf83` `回收 SQLite 暫時性故障連線並重試`
- Diff stat:
  - `src/memory_hall/storage/sqlite_store.py` `411 insertions, 138 deletions`
  - `tests/test_reindex_retry.py` `149 insertions, 1 deletion`
- Main changes:
  - `src/memory_hall/storage/sqlite_store.py:18` added transient `sqlite3.OperationalError` markers for `disk i/o error`, `database is locked`, `database table is locked`, and `database is busy`.
  - `src/memory_hall/storage/sqlite_store.py:626` added `_run_read_operation()` with one-shot recycle + retry.
  - `src/memory_hall/storage/sqlite_store.py:648` added `_run_writer_operation()` with writer recycle + reopen before retry.
  - `src/memory_hall/storage/sqlite_store.py:670` logs recycled connection ids via `aiosqlite connection recycled after disk I/O error ...`.
  - `tests/test_reindex_retry.py:142` verifies writer connection recycle on first `disk I/O error`.
  - `tests/test_reindex_retry.py:214` verifies read connection recycle on first `database is locked`.

## Patch C

- Commit: `3e7d2ce` `強制 runtime sqlite3 系統連結指向 3.53.0`
- Diff stat:
  - `Dockerfile` `6 insertions, 1 deletion`
- Main changes:
  - `Dockerfile:83` keeps `/opt/sqlite/lib` in `ld.so.conf.d`, runs `ldconfig`, and forces `/lib/aarch64-linux-gnu/libsqlite3.so.0` to point at `/opt/sqlite/lib/libsqlite3.so.3.53.0`.
  - `Dockerfile:85` adds the rationale comment for child processes that do not inherit `LD_LIBRARY_PATH`.

## Patch D

- Commit: `a19886b` `新增背景 WAL checkpoint 與雙資料庫截斷`
- Diff stat:
  - `src/memory_hall/config.py` `1 insertion`
  - `src/memory_hall/server/app.py` `41 insertions`
  - `src/memory_hall/storage/interface.py` `2 insertions`
  - `src/memory_hall/storage/sqlite_store.py` `111 insertions, 20 deletions`
  - `src/memory_hall/storage/vector_store.py` `182 insertions, 79 deletions`
  - `tests/test_smoke.py` `75 insertions`
- Main changes:
  - `src/memory_hall/config.py:38` adds `wal_checkpoint_interval_s` mapped from `MH_WAL_CHECKPOINT_INTERVAL_S`, default `300.0`.
  - `src/memory_hall/server/app.py:105` adds `_wal_checkpoint_worker`.
  - `src/memory_hall/server/app.py:388` adds the periodic background task.
  - `src/memory_hall/server/app.py:736` adds `_checkpoint_wal_databases()` for both main DB and vector DB with `WAL checkpoint completed: ...` logging.
  - `src/memory_hall/storage/sqlite_store.py:493` adds async `checkpoint_wal()` with reader gating plus `PASSIVE -> RESTART -> TRUNCATE`.
  - `src/memory_hall/storage/sqlite_store.py:706` adds `_acquire_reader_slot()` / `_pause_readers()` so checkpoint can drain in-flight readers before truncation.
  - `src/memory_hall/storage/vector_store.py:50` adds a connection lock around the shared sqlite3 connection.
  - `src/memory_hall/storage/vector_store.py:163` adds vector DB `checkpoint_wal()`.
  - `tests/test_smoke.py:151` writes `100` entries, triggers checkpoint directly, and verifies both WAL files shrink to `<= 32KB`.

## Pytest Results

- After Patch A: `42 passed, 1 skipped, 4 warnings`
- After Patch B: `44 passed, 1 skipped, 4 warnings`
- After Patch C: `44 passed, 1 skipped, 4 warnings`
- After Patch D: `45 passed, 1 skipped, 4 warnings`
- Extra targeted runs:
  - `pytest -q tests/test_reindex_retry.py`
  - `pytest -q tests/test_smoke.py tests/test_vec0.py`
  - `ruff check src tests`

## Deviations

- Patch C docker validation could not be completed in this sandbox:
  - `docker build -t memory-hall:0.1.0 .`
  - Result: `permission denied while trying to connect to the docker API at unix:///Users/maki/.docker/run/docker.sock`
- I did not make a 5th commit for this summary file, to preserve the requested 4-patch commit structure.
- I could not update `~/infrastructure/handoff/latest.md` from this session because the sandbox only allows writes inside the repo/workspace roots.

## Latent Bugs Found

- `src/memory_hall/storage/sqlite_store.py:47`
  - `SqliteStore.open()` still fails hard if a transient SQLite error happens during startup schema/open, because the new recycle+retry layer only wraps steady-state read/write operations, not bootstrap.
- `src/memory_hall/storage/vector_store.py:50`
  - Shared vector DB connection had no explicit locking before this PR. I fixed that as part of Patch D because the new checkpoint path made the race impossible to ignore.
