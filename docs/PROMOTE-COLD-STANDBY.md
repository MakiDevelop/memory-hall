# memhall Cold Standby Promote Runbook

> 狀態：2026-07-24 Phase 1（R3 cold standby）+ Council DL-4 合併（fence / integrity gate / snapshot）  
> 主（live）：mini2 `100.89.41.50:9100`  
> 備（cold）：mini1 `100.122.171.74:9100` — **平時 container 停、不監聽**  
> 本檔為唯一正典；`docs/operations/promote-runbook.md` 已改為 pointer。

## 何時用

僅當 **主 mini2 確認不可達**，且需要恢復寫入/讀取服務時。  
**禁止** agent 自動切備機；必須人類（Maki）確認。

## 前提

- 備機 cron 每 ~5 分鐘 rsync 主庫（RPO ≈ 5 min）
- 備機 memory-hall container **必須在 cold 期間保持停止**（rsync 覆蓋開啟中 SQLite 會損壞）
- MacBook Pro 不跑常駐服務

## Promote 最小步驟

### 1. 確認主真的掛了

```bash
# 從本機或任一內網節點
curl -s -m 5 -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $MH_API_TOKEN" \
  "http://100.89.41.50:9100/v1/memory?limit=1"
# 預期：非 200（timeout / 000 / 5xx）
```

第二來源（建議）：`ssh mini2-ts 'docker ps | grep memory-hall'` 或 ping Tailscale。
記錄「最後一次成功寫入時間」與 mini1 backup log 最後 ok 時間（估 RPO 用）。

### 1.5 Fence 舊主（防 split-brain — Council DL-4）

mini2 仍可 SSH → 先停它的 memhall container；整機不可達 → 確認已關機/隔離。
同時停止所有 agent 對 mini2 的寫入 retry。
**風險**：`:9100` timeout 不代表 mini2 不會稍後復活繼續收寫入；未 fence 即促轉 = 雙主。

### 2. 停備機 rsync cron（防腦裂覆寫）

在 **mini1**：

```bash
ssh mini1-ts 'crontab -l | sed "s|^\\(.*memhall-backup.*\\)|# PROMOTE-HOLD \\1|" | crontab -'
# 或暫時：mv ~/bin/memhall-backup-from-mini2.sh ~/bin/memhall-backup-from-mini2.sh.hold
```

**順序依賴**：必須先停 cron，再啟動 container。否則 cron 可能用「已死主機」的舊資料蓋掉剛轉正的備機。

### 3. 檢查備機資料檔可接受 + 一致性硬閘門（Council DL-4）

```bash
ssh mini1-ts 'ls -la ~/data/memory-hall/ | head -20'
# 確認 mtime 接近最近一次成功 rsync（通常 ≤ 5–10 分鐘前，若主已掛則以最後成功時間為準）
```

先做一份**不覆蓋原檔**的 snapshot（保留事故現場 + rollback point），再對兩庫跑 integrity check：

```bash
ssh mini1-ts 'cp -a ~/data/memory-hall ~/data/memory-hall.pre-promote-$(date +%Y%m%d%H%M)
  sqlite3 ~/data/memory-hall/memory-hall.sqlite3 "PRAGMA integrity_check;"
  sqlite3 ~/data/memory-hall/memory-hall-vectors.sqlite3 "PRAGMA integrity_check;"'
```

**任一結果非 `ok` → 停止促轉**，改用更早的 snapshot；不可猜測式修復（不用 `.recover` 硬拉）。
理由：cron rsync 的來源是運行中的 SQLite，「rsync 成功」不保證跨檔交易一致（Council 2026-07-24，Codex/Gemini 獨立收斂）。

### 4. 啟動 mini1 memory-hall

```bash
ssh mini1-ts 'cd ~/GitHub/memory-hall && docker compose up -d'
# 實際 path / compose 名稱以機上為準
```

先只讀驗證（container log、`/v1/health`、list 讀既有資料），**不做測試寫入**；寫入等 Step 6 Maki 確認切換後，再寫一筆可辨識 test entry 並讀回。

### 5. Health check 備機

```bash
curl -s -m 5 -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $MH_API_TOKEN" \
  "http://100.122.171.74:9100/v1/memory?limit=1"
# 預期：200
```

### 6. 切 client 主 URL

更新（並 **Maki 確認**）：

| 位置 | 改為 |
|------|------|
| `~/.amh/config.json` `store_path` | `http://100.122.171.74:9100` |
| skills / SHARED 文件主 IP | mini1（並標「臨時 promote」） |

### 7. 公告

告訴所有 agent session：目前主 = mini1 cold-promoted；勿再打 mini2。

### 8. 原主修復後（反向同步 — 需另簽）

1. 修 mini2  
2. **停寫** 或接受短暫只讀  
3. 決定資料方向（通常 mini1 → mini2 一次 rsync）  
4. 啟動 mini2、health  
5. client 切回 mini2  
6. 停 mini1 container、**恢復** rsync cron  

此步涉及資料方向，**RED**：需 Maki 明確確認。

## 停止條件（任一命中即中止促轉、保留現場 — Council DL-4）

- 無法 fence mini2
- cron 無法確認已停
- 任一庫 `integrity_check` 非 ok
- bind mount / compose 目錄指向不明
- 資料基線明顯落後且不可接受

## 風險

| 風險 | 緩解 |
|------|------|
| 未停 cron 導致覆寫 | Step 2 強制 |
| rsync 快照跨檔不一致 | Step 3 integrity check 硬閘門 + snapshot（根治靠 DL-6 快照化備份，defer 中） |
| RPO 內資料遺失 | 接受 cold RPO≈5min；重要寫入另有 session dir / 交接包 |
| 雙主腦裂 | Step 1.5 fence + 禁止同時 live 兩台寫入 |
| 文件未更新 | promote 當日更新 SHARED + quick-ref |

## 相關

- Backup script: `deploy/memhall-backup.sh`（應在備機跑，SRC=主 mini2）
- Health probe: `scripts/memhall-health-probe.sh`
- 優化規劃: `~/Documents/agent-council/2026-07-24-amh-team-memhall-optimization/`
