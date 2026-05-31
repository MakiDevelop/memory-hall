# ADR 0009 — Admin gate（two-tier bearer，不做 HMAC）

- **Status**: Accepted
- **Date**: 2026-04-28
- **Related**: ADR 0007（minimal token auth，這份是它的最小延伸）、ADR 0008（personal PKI 輕量立場，是這份的判準依據）、Codex Phase B Dissent 2026-04-27（D2 Option E 的最小實作）

## Context

現況下 `/v1/admin/reindex` 與 `/v1/admin/audit` 兩個 admin endpoint 由 `MH_API_TOKEN` 統一保護——任何持有 api_token 的 caller 都能呼叫 admin 操作。風險：

- api_token 被多個 caller 共用（ops-hub / repo CLI / 4 個 Claude skills / mk-brain），任一 caller 機器被攻破或 log 不慎洩漏 token，都直接拿到 admin 權限
- reindex 是危險動作（會掃描全表、可能踩到 embedder 連環失敗），不該與一般 read/write 共用權限

七位一體 Phase B 一開始的提案是「HMAC + nonce + replay window + principal registry + 14 天並存期 + 7 連日零 bearer write 退場」一整套 production-grade machinery（rules/agent-security-hygiene.md S2.1 的方向）。Codex Phase B Dissent 2026-04-27 D2 Option E 把它縮成「先封 admin，再做 attribution」。SuperGrok 2026-04-28 sanity check：2025-2026 全球範圍沒有命中本情境（Tailscale tailnet + single-tenant + two-tier static bearer）的近期 incident，社群也沒把這個簡化設計列為已知 anti-pattern；獨立 admin bearer 反而是 community 推薦的 least-privilege 做法。

ADR 0008 已 ratify「memhall 是 personal PKI，輕量 > 完整」，明確排除 HMAC / principal registry / per-key rotation。本 ADR 把 Phase B 縮到 ADR 0008 立場下還能做的最小步驟。

## Decision

**新增 `MH_ADMIN_TOKEN`（optional，獨立於 `MH_API_TOKEN`）。設定後 `/v1/admin/*` 要求 admin token，一般 api_token 在 admin path 上被拒絕。**

- 新 config field：`Settings.admin_token: str | None = None`（`MH_ADMIN_TOKEN` env）
- Middleware 行為（`src/memory_hall/server/app.py` 的 `require_api_token`）：
  - `/v1/health*` → 永遠 public（沿用 ADR 0007）
  - `/v1/admin/*` 且 `admin_token` 已設 → 要求 `Authorization: Bearer <admin_token>`，傳 `api_token` 也回 `401`
  - `/v1/admin/*` 但 `admin_token` 未設 → fallback 到 `api_token` 邏輯（ADR 0007 backward compat）
  - 其他 path → 既有 `api_token` 邏輯
- `admin_token` 不能反過來用在非 admin path（least privilege 雙向）
- 比較全程用 `hmac.compare_digest`（constant-time，沿用 ADR 0007）
- 錯誤訊息分開（`invalid token` vs `invalid admin token`），但**不**用 `403` 區分「你的 token 是 valid api_token 但不是 admin」——避免 token validity oracle

非程式碼層面的搭配（docs only，不寫進 repo code）：

- 在 mini Tailscale ACL 鎖 `/v1/admin/*` path 到 Maki 自己的 device（defense-in-depth 第二層）
- Token 用 `openssl rand -hex 32` 生成，與 `MH_API_TOKEN` 不同值
- 不要 log `Authorization` header（已 grep 過 src/memory_hall/，目前無此類 log；本 PR 不引入）

## Consequences

### Gains

- **Admin 操作從共享 token 隔離出來**：一般 caller token 洩漏不再等於 admin 失守
- **Backward compatible**：`MH_ADMIN_TOKEN` 未設時行為與 ADR 0007 完全相同，現有 deployment 不需改
- **實作 ~30 行**（config 1 行 + middleware 改 ~20 行 + tests 6 個 case），1.5 小時內完成
- **Personal PKI 體檢通過**：1 個新 config knob、0 個新 schema 欄位、0 個跨組織機制

### Costs

- **仍是 possession-based**：admin_token 洩漏 = admin 失守，沒有 cryptographic attribution
- **沒有 rotation infra**：rotate admin_token = 改 env + restart container + 通知少數 caller，與 api_token 同等
- **Config-load 時 fail-fast 兩個 invariant**（Codex review 2026-04-28 PR1 round 1 補強，5 行 pydantic validator）：
  - `admin_token` 設了但 `api_token` 沒設 → 拒絕啟動（否則非 admin path 會 fail-open）
  - `admin_token == api_token` → 拒絕啟動（否則 two-tier 被靜默抵消）
  - 這兩條不算違反 ADR 0008 輕量原則：屬於「防止操作者誤配置造成 silent security regression」，5 行 code 防一個 high-severity 漏洞，ROI 明確

### Non-goals

- 不取代 HMAC（rules/agent-security-hygiene.md S2.1 仍是 destination，但 sunset criteria 未觸發）
- 不引入 principal registry / role mapping
- 不做 14 天 sunset window（沒有要 retire 的舊機制）
- 不在 code 層強制 Tailscale ACL（infra config 該由 ops 維護）

## Alternatives considered

### A. Codex 完整版 Phase B Option E（registry + HMAC + 14 天並存期 + 7 連日零 bearer write 退場）

拒絕：sunset criteria 未觸發（單一 operator / caller < 10 / 全部在 Maki tailnet 內）。引入 HMAC 等 ADR 0008 sunset criteria 1 (第二個 operator) 或 5 (token 洩漏 incident) 之一發生才做。

### B. 用 `403 Forbidden` 區分「valid api_token 用在 admin path」

拒絕：會形成 token validity oracle（攻擊者送 garbage 拿 401，送 valid api_token 拿 403，能反推 token 是否合法）。統一回 401 較安全。內部 caller 的 debug 體驗用「invalid admin token」訊息字串足以區分。

### C. 不做 admin gate，靠 Tailscale ACL 鎖 path

拒絕：ACL 是 device 層級，無法區分「同 device 上 ops-hub 的 read-only flow」和「同 device 上不該呼叫 reindex 的 LINE bot」。code 層 self-defense + ACL defense-in-depth 比單靠 ACL 強。

### D. 把 admin_token 設成 default required（不向後相容）

拒絕：會影響現有 deployment（mini production），需要 migration window。本 ADR 走可逆路徑：opt-in 起手，未來如果要強制可再 supersede。

## Sunset criteria

任一條件成立就重新審視：

1. ADR 0008 任一 sunset criteria 觸發（自動帶動本 ADR）
2. admin_token 洩漏 incident（這份 ADR 為什麼沒有 rotation infra 就是答案——出事的話 rotation 是第一個要建的東西）
3. caller 數量需要 per-caller admin attribution（例如知道是 ops-hub 還是 mk-brain 觸發的 reindex）
4. 出現第三層權限需求（read-only / write / admin → read-only / write / reindex / audit / superuser）

## Implementation summary

- `src/memory_hall/config.py`：加 `admin_token` 欄位
- `src/memory_hall/server/app.py`：擴充 `require_api_token` middleware，加 admin path 分支
- `tests/test_auth.py`：8 個新 case（6 個 middleware 行為 + 2 個 config invariant fail-fast）
- `.env.example`：加 `MH_ADMIN_TOKEN=` 範例段落
- `docs/api.md`：加「Admin gate (two-tier bearer)」段落

Total: ~140 行 across 6 files。`pytest`：16 passed (auth)，full suite 59 passed 1 skipped。

## Round 1 review history

- 2026-04-28 Codex review REJECT，2 finding：
  1. [HIGH] `admin_token` 設 + `api_token` 沒設 → 非 admin path fail-open（實測 POST /v1/memory/write 回 201）
  2. [MEDIUM] `admin_token == api_token` → 靜默抵消 two-tier
- 修法：在 `Settings` 加 `_validate_auth_tokens` model_validator，config load 時 fail-fast
- 補 2 個 unit test 鎖 invariant
