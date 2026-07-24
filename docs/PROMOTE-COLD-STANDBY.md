# memhall Cold Standby Promote Runbook

> 狀態：2026-07-24 Phase 1（R3 cold standby）  
> 主（live）：mini2 `100.89.41.50:9100`  
> 備（cold）：mini1 `100.122.171.74:9100` — **平時 container 停、不監聽**

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

### 2. 停備機 rsync cron（防腦裂覆寫）

在 **mini1**：

```bash
ssh mini1-ts 'crontab -l | sed "s|^\\(.*memhall-backup.*\\)|# PROMOTE-HOLD \\1|" | crontab -'
# 或暫時：mv ~/bin/memhall-backup-from-mini2.sh ~/bin/memhall-backup-from-mini2.sh.hold
```

**順序依賴**：必須先停 cron，再啟動 container。否則 cron 可能用「已死主機」的舊資料蓋掉剛轉正的備機。

### 3. 檢查備機資料檔可接受

```bash
ssh mini1-ts 'ls -la ~/data/memory-hall/ | head -20'
# 確認 mtime 接近最近一次成功 rsync（通常 ≤ 5–10 分鐘前，若主已掛則以最後成功時間為準）
```

### 4. 啟動 mini1 memory-hall

```bash
ssh mini1-ts 'cd ~/GitHub/memory-hall && docker compose up -d'
# 實際 path / compose 名稱以機上為準
```

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

## 風險

| 風險 | 緩解 |
|------|------|
| 未停 cron 導致覆寫 | Step 2 強制 |
| RPO 內資料遺失 | 接受 cold RPO≈5min；重要寫入另有 session dir / 交接包 |
| 雙主腦裂 | 禁止同時 live 兩台寫入 |
| 文件未更新 | promote 當日更新 SHARED + quick-ref |

## 相關

- Backup script: `deploy/memhall-backup.sh`（應在備機跑，SRC=主 mini2）
- Health probe: `scripts/memhall-health-probe.sh`
- 優化規劃: `~/Documents/agent-council/2026-07-24-amh-team-memhall-optimization/`
