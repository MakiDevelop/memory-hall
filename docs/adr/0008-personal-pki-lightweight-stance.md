# ADR 0008 — memhall 是 personal PKI，輕量 > 完整

- **Status**: Accepted
- **Date**: 2026-04-28
- **Related**: ADR 0003（engine library vs deployment platform）、ADR 0005（v0.2 minimum viable contract）、ADR 0007（minimal token auth）、`rules/four-layer-north-star.md` L4

## Context

2026-04-28 對 Phase A / A.5 / B 體檢時發現一個漂移傾向：每個 reliability incident 後，patch 容易順手帶入「業界最佳實踐」（k8s liveness/readiness 拆分、weighted linear hybrid 加 tuning knob、HMAC + principal registry + key rotation），把 memhall 的複雜度往 production-grade memory platform 推。

但 memhall 的實際定位是：

- **單一使用者**（Maki）
- **單一部署**（Mac mini Tailscale tailnet `:9100`，mini2 冷備）
- **規模 ~10² 量級** entries
- **caller < 10**（ops-hub / repo CLI / `.claude/skills/*` / mk-brain），全部在 Maki 自己的 tailnet 內
- **目的**：七位一體共用記憶大廳 + Maki 個人 PKI 的聯想入口

ADR 0003 已經把「engine library vs deployment platform」分開——這份 ADR 把它再往前推一步，明確 memhall 的設計目標**不是** production-grade memory platform，是 **personal PKI 的記憶引擎**。

## Decision

memhall 接受以下四個北極星，依優先序：

1. **聯想品質**（retrieval recall / ranking 正確）
2. **穩定**（不會壞、不會吞錯、不會 silent degrade）
3. **快速**（search p50 < 200ms，write < 50ms）
4. **輕量**（schema、config knob、auth 機制、ops surface 都要可以一個人理解）

**任何 patch 在 land 前必須通過「personal PKI 體檢」**：

- 這個改動修的是真 bug 還是引入「業界慣例」？
- 加了幾個 config knob？每個 knob 的 default 你能解釋嗎？
- schema 多了幾個欄位？對 ~10² 規模值得嗎？
- 對單一 caller 場景，是否引入跨組織 / 多 tenant / 多 operator 才需要的機制？
- 如果回答「以後可能用得到」——拒絕，等真的用到再做。

明確**不做**的清單（除非觸發 sunset criteria）：

- ❌ k8s 風格的 liveness/readiness/startup probe 三件套（單一 launchd container 不需要）
- ❌ Hybrid search 的可調 α / mode switch（除非有 retrieval benchmark 證明非 RRF 更好）
- ❌ HMAC + nonce + per-key rotation（ADR 0007 minimal token + Tailscale ACL 已足夠）
- ❌ Principal registry / role mapping / `key_id → role/ns/agent` 表
- ❌ Per-row 失敗計數 / retry budget machinery（log + 下次 reindex 重試就夠）
- ❌ Dashboard / metrics aggregation / 需要打開看的觀測介面（違反 L4）

## Consequences

### Gains

- **複雜度預算用在聯想品質上**（embedding 模型、ranking、CJK tokenization），不是 ops surface
- **單人可維護**：schema、auth、health 邏輯都能一個下午讀完
- **可逆**：每個 ADR 都有 sunset criteria，跨過門檻就升級，不跨就保持輕量
- **OSS friendly**：`git clone && docker compose up` 立刻能跑，不需要設 ACL / 簽 cert / 發 key

### Costs

- **不適合多 operator 共用**：第二個 operator 出現時，這份 ADR 的多數決策需要重新評估
- **Audit trail 較弱**：自我宣告的 `agent_id` 是唯一的 attribution，不是密碼學保證
- **某些「正確」的工程實踐被刻意延後**：HMAC、principal registry、retry budget——不是因為它們錯，是因為**現在做的 ROI 不夠**

### Non-goals

- 不取代 ADR 0003 的 engine vs platform 分工：production-grade ACL / multi-tenant ACL / 跨組織 audit 仍由未來的 `memory-gateway` 承擔
- 不否定 `rules/agent-security-hygiene.md` S2.1 的 HMAC 規格——那是 destination，這份 ADR 是「現在不要走」的理由
- 不放棄 reliability：Phase A SQLite chain / silent except / WAL 修復都是必須做的，這份 ADR 不是「拒絕修 bug」

## Sunset criteria

任一條件成立就重新審視這份 ADR：

1. 第二個 operator（不是 Maki）開始寫入同一個 memhall 部署
2. caller 數量 > 20，或出現 Maki 不認識的 caller
3. entries 規模超過 10⁵（schema / index 策略可能需要重新設計）
4. 出現需要密碼學 attribution 的 incident（token 洩漏 + 不知道誰寫的 entry）
5. memhall 變成 OSS 多人協作專案，外部 contributor 開始要求「production-grade」feature

## Alternatives considered

### A. 不寫這份 ADR，用 PR review 把關

拒絕。沒有明文化的設計哲學，每個 PR 都要重新辯論「這個是不是 over-design」。這份 ADR 把判準寫下來，未來的 PR / Codex 提案 / Claude 設計都先過這份體檢，不通過就直接砍。

### B. 寫成 rule（`rules/memhall-lightweight.md`）而非 ADR

拒絕。ADR 是 repo 內 immutable 決策記錄，scope 限定 memhall。Rules 是跨專案行為規範。這份內容的 scope 是 memhall 設計哲學，屬於 ADR。

### C. 列「禁止做什麼」清單但不寫優先序

拒絕。沒有優先序時，遇到取捨會憑感覺。明確「聯想品質 > 穩定 > 快速 > 輕量」讓未來的取捨有依據——例如 BM25 normalize bug 雖然動了 ranking 邏輯，但是修聯想品質，最高優先；hybrid α 參數化是輕量倒退，最低優先，需要 benchmark 才能 land。

## Implementation summary

- 新增本 ADR
- 更新 `docs/adr/README.md` 索引
- 後續 PR description 在引入新 config knob / schema 欄位 / auth 機制時，必須引用本 ADR 並回答「personal PKI 體檢」五題
