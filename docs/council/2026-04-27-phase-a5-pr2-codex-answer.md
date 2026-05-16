# 2026-04-27 Phase A.5 PR2 Codex Summary

Source briefing: [AGENT: Claude]

Note:
本次 sandbox 可修改工作樹與跑測試，但無法在 `.git/` 建立新檔，`git commit` 會失敗於 `.git/index.lock: Operation not permitted`。
以下內容因此依「預期 commit 邊界」整理，而不是實際 commit hash。

## Intended Commit 1 — Patch E.0

- Intended message:
  - `fix(search): 修 _normalize_bm25 邏輯反轉 (BM25 愈好 score 反而愈低)`
- Files:
  - `src/memory_hall/storage/sqlite_store.py`
  - `tests/test_fts_tokenization.py`
- Main changes:
  - `src/memory_hall/storage/sqlite_store.py:839` 將 BM25 normalize 從 `1/(1+abs(s))` 改為 `-bm25/(1.0-bm25)`，使 SQLite FTS5 的負值 BM25 與品質同向單調遞增。
  - `tests/test_fts_tokenization.py:100` 新增 5 個負值 BM25 的純單元測試，驗證 normalize 後排序保序。
  - Caller audit 結論：`_normalize_bm25()` 目前只有 `SqliteStore.search_lexical()` 使用；它先由 SQL `ORDER BY bm25_score`（raw negative BM25，越小越好）決定 lexical 排名，再把 normalized score 傳給 `MemoryHallRuntime.search_entries()`，沒有其他 caller 另外拿 normalized 分數做反向排序。
- Pytest after patch:
  - `46 passed, 1 skipped, 4 warnings`

## Intended Commit 2 — Patch E

- Intended message:
  - `feat(search): hybrid 改 weighted linear combination (α=0.3 default)`
- Files:
  - `src/memory_hall/config.py`
  - `src/memory_hall/models.py`
  - `src/memory_hall/server/app.py`
  - `tests/test_search_degraded.py`
  - `tests/test_hybrid_search.py`
- Main changes:
  - `src/memory_hall/config.py:36` 新增 `MH_HYBRID_MODE`（default `weighted_linear`，可切 `rrf`）與 `MH_HYBRID_ALPHA`（default `0.3`）。
  - `src/memory_hall/server/app.py:263` search path 改為依 mode dispatch。
  - `src/memory_hall/server/app.py:789` 新增 weighted linear combine：`alpha * bm25 + (1 - alpha) * semantic`。
  - `src/memory_hall/server/app.py:796` 邊界條件：
    - `semantic_status != "ok"` → `alpha=1.0` pure lexical fallback
    - lexical 空 → `alpha=0.0` pure semantic
    - no overlap → union 補 `0.0`
  - `src/memory_hall/server/app.py:816` 保留 legacy RRF path，`MH_HYBRID_MODE=rrf` 可切回舊行為。
  - `src/memory_hall/models.py:183` `score_breakdown` 新增 `hybrid_mode` 與 `alpha`。`rrf` 欄位名稱保留給 backward compatibility，但值現在承載最終 combined score，需配合 `hybrid_mode` 解讀。
  - `tests/test_hybrid_search.py` 新增 4 個測試：
    - rare lexical target Top 1
    - semantic paraphrase 在 lexical 空時由 semantic 拉回
    - conflict resolution 時 both-signals entry 高於單維 entry
    - legacy `rrf` mode 仍可用
  - `tests/test_search_degraded.py` 補 semantic fail 時 `alpha=1.0` pure lexical fallback 驗證。
- Pytest after patch:
  - `50 passed, 1 skipped, 4 warnings`
- Extra targeted runs:
  - `pytest -q tests/test_hybrid_search.py tests/test_search_degraded.py tests/test_write_search.py tests/test_cjk_search.py`

## Intended Commit 3 — Patch F

- Intended message:
  - `feat(health): 拆分 liveness /healthz 與 readiness /ready (業界慣例)`
- Files:
  - `src/memory_hall/server/app.py`
  - `src/memory_hall/server/routes/health.py`
  - `tests/test_smoke.py`
  - `tests/test_auth.py`
  - `Dockerfile`
- Main changes:
  - `src/memory_hall/server/app.py:342` 新增 `ready()`，沿用既有 `_health_cache` 與 sub-check。
  - `src/memory_hall/server/app.py:348` 新增 `healthz()`，固定回 `{"status": "alive"}`，不碰 DB / vector / embedder。
  - `src/memory_hall/server/routes/health.py` 新增 `/v1/healthz`、`/v1/ready`，並讓 `/v1/health` alias 到 readiness 回應。
  - `src/memory_hall/server/app.py:953` auth middleware 放行 `/v1/healthz`、`/v1/ready`、`/v1/health`。
  - `Dockerfile:115` `HEALTHCHECK` 改打 `/v1/healthz`，避免 readiness probe 因 embedder/reindex 波動而誤判 container dead。
  - `tests/test_smoke.py` 新增：
    - `healthz` 在 embedder fail 時仍回 200 alive
    - `/v1/ready` 與 `/v1/health` alias response 一致
    - Dockerfile probe 指向 `healthz`
  - `tests/test_auth.py` 更新為 3 個 health endpoints 都保持 public。
- Pytest after patch:
  - `53 passed, 1 skipped, 4 warnings`
- Extra targeted runs:
  - `pytest -q tests/test_smoke.py tests/test_auth.py`

## Deviations

- 無法在此 session 內完成使用者要求的 3 個實際 git commits：
  - `git add` 可行
  - `git commit` 失敗：`.git/index.lock: Operation not permitted`
- 無法更新 `~/infrastructure/handoff/latest.md`：
  - 該路徑不在本 session writable roots 內
- 未引入新 dependency。
