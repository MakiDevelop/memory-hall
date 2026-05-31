# Codex Phase B Dissent

## D1 反對點
- 不同意叫 Option C。B.3 defer 後，這次其實不是 defense-in-depth；E26 仍是 app-layer single fence。`/v1/admin/*` 的 Tailscale ACL 應升到同優先。
- HMAC 不算 over-engineering，E11 attribution 需要它；但順序錯。`Principal.role` 現在預設就是 `admin`，role source 不存在。若 B.1 先做、B.2 後補，容易變成所有 valid key 都是 admin。E25 也證明 bearer 與 principal chain 斷開。
- caller 成本被低估：`ops-hub` 只會 bearer 且 401/403 當 permanent；repo CLI 不帶 auth；`.claude/skills/*` 是 curl bearer；`mk-brain` 的 HMAC 是 gateway 另一套格式。

## D2 替代方案
- **Option E**：先封 admin，再做 attribution。E0 用現有 bearer + static allowlist + Tailscale ACL 鎖 `/v1/admin/*`；E1 補 principal registry（`key_id -> role/ns/agent`）+ shared signer，再漸進切 S2.1；E2 telemetry 穩定後 retire bearer。JWT/PASETO 不建議，沒解 replay/body integrity。

## D3 Missing Risks
- R6 role model 缺席；R7 rotation 期間 `ops-hub` queue 遇 401/403 會 drop queued records；R8 220 筆 `dev-local` 不可硬回填，應標 `legacy-unattributed`。

## D4 Implementation Order
- 先 admin gate，再 HMAC。拆兩 PR：PR1 admin gate + tests + ACL；PR2 registry + HMAC + caller helper。並存期至少 14 天，且以 7 連日零 bearer write 退場，不建議寫死 7 天。

## D5 mk-council Interaction
- bypass 只繞過 council，不等於 auth downgrade。direct memhall 仍應優先 HMAC；bearer 只能在 sunset window 做 non-admin fallback，`/v1/admin/*` 不得 fallback。ADR-0007 可暫留為 deprecated shim，adoption 完成後 retire。

## Verdict
**APPROVE WITH MODIFICATIONS** — 方向對，但若不先封 E26、先補 role/registry，再推 migration，只是把洞從「無 gate」換成「所有 valid key 皆 admin」。
