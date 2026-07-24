# memhall Promote Runbook — mini1 冷備轉正

> 來源：2026-07-24 Council（session: `~/Documents/agent-council/2026-07-24-memhall-standby-topology/`，DL-4）
> 適用：主 memhall（mini2 `100.89.41.50:9100`）確診故障，需把冷備 mini1（`100.122.171.74`）轉正。
> 原則：**永不自動 promote**。整條 runbook 由人工執行，第 6 步切 endpoint 前必經 Maki ratify。

## 拓撲現況（2026-07-24）

- 主：mini2，docker compose（`memory-hall-memory-hall-1`），SQLite main + vectors 兩庫，bind mount 資料目錄
- 冷備：mini1，container 平時**停止**、port 9100 不監聽；cron `*/5 * * * * ~/bin/memhall-backup-from-mini2.sh` 從 mini2 rsync WAL 三件套到 `~/data/memory-hall/`
- ⚠️ 已知未驗證項（DL-7）：mini1 實機 backup script 與 repo 版 `deploy/memhall-backup.sh` 是否同構；rsync 活庫快照的交易一致性（DL-6 快照化 defer 中）

## 七步驟（順序不可換）

### 1. 確認確為主機故障，不是網路誤判
多點驗證（MBP + 另一台機器都打不通 mini2:9100；Tailscale ping；SSH 是否可達）。記錄最後一次成功寫入時間與 mini1 backup log 最後 ok 時間。
**理由**：誤促轉會製造雙主。**風險**：只看到單一 client timeout 就促轉。

### 2. Fence mini2（確保舊主不再接受寫入）
mini2 可控 → SSH 停 memhall container；整機不可達 → 確認其已關機/隔離。同時停止所有 agent 對 mini2 的寫入 retry。
**理由**：必須先建立 single-writer。**風險**：`:9100` timeout 不代表 mini2 不會稍後復活繼續收寫入 → split-brain。

### 3. 停 mini1 rsync cron，並確認無執行中同步
```bash
crontab -l > ~/crontab.bak-$(date +%Y%m%d)   # 先備份
crontab -e   # 註解 memhall-backup-from-mini2.sh 該行
pgrep -fl rsync   # 確認沒有進行中的同步
```
**理由**：必須早於起 container，否則 cron 會用舊主資料覆蓋剛轉正的 DB。**風險**：漏停 = 新寫入 5 分鐘內被覆蓋，災難性資料倒退。

### 4. 凍結並驗證候選資料（硬閘門）
container 保持停止。記錄兩庫 + WAL/SHM 時間戳與大小 → 做一份**不覆蓋原檔**的 snapshot（保留事故現場與 rollback point）→ 兩庫各跑：
```bash
sqlite3 ~/data/memory-hall/memory-hall.sqlite3 'PRAGMA integrity_check;'
sqlite3 ~/data/memory-hall/memory-hall-vectors.sqlite3 'PRAGMA integrity_check;'
```
**任何結果非 `ok` → 停止促轉**，改用更早的 snapshot，不可猜測式修復（不用 `.recover` 硬拉）。
**理由**：rsync 成功 ≠ 跨檔交易一致。**風險**：snapshot 需足夠磁碟。

### 5. 起 mini1 container，先只讀驗證
在核實過的 compose 目錄（`~/GitHub/memory-hall/`，確認 `MEMHALL_DATA_DIR` 指向 `~/data/memory-hall/`）`docker compose up -d`。查 container log、health endpoint、AMH read/list。**不做測試寫入**。
**理由**：先證明能讀既有資料。**風險**：錯誤 compose project 或 bind mount 會起一顆空 DB。

### 6. 核對資料基線 → **Maki ratify** → 切 endpoint
抽查最近記憶與總量，確認 RPO ≤5 分鐘可接受。批准後把 skill/agent 的 active endpoint 切到 `100.122.171.74:9100`，寫一筆可辨識的測試 entry 並讀回。
**理由**：endpoint 切換 = 正式建立新 writer 的時刻。**風險**：未核對就寫入，rollback/merge 更難。

### 7. 降級運行 + failback 另案
rsync cron 保持停用（mini1 已是主，不得再被覆蓋）。記錄 promotion 時點、最後同步時點、資料缺口。mini2 恢復後其 memhall 保持停止，reverse-seed / failback 計畫另案經 Maki ratify。
**理由**：舊主恢復 ≠ 可直接回歸；必須先決定資料流向。**風險**：自動恢復舊 cron 或舊主服務 = 覆寫/雙主。

## 停止條件（任一命中即中止促轉、保留現場）

- 無法 fence mini2
- cron 無法確認已停
- 任一庫 `integrity_check` 非 ok
- bind mount / compose 目錄指向不明
- 資料基線明顯落後且不可接受

## 監控（DL-5）

n1k.tw（Tailscale）cron-curl 每分鐘探測 mini2:9100 health，連續 3 次失敗 → Telegram 告警，恢復再通知一次。**告警只通知，永不觸發自動 promote。**
