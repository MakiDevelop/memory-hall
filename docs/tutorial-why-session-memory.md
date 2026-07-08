# 為什麼你的 AI 助手需要 Session Memory？

> 你有沒有這種經驗：跟 Claude 工作了兩小時，關掉對話窗，  
> 隔天再開——它完全不記得昨天做了什麼。

這篇教學說明 **session memory** 解決什麼問題、有哪些做法，以及怎麼用最少的設定讓你的 Claude 開始「記住」。

---

## 問題：每次對話都從零開始

Claude Code 的 context window 很大，但它有一個根本限制：**session 結束，記憶歸零**。

這帶來三個痛點：

### 1. 重複交代背景

每次開新 session，你得重新解釋：
- 專案架構長什麼樣
- 上次改到哪裡
- 為什麼做了某個決定

10 分鐘的 context 交代，一天開三次 session 就浪費半小時。

### 2. 決策失憶

上週你和 Claude 討論了 30 分鐘，最後決定用 A 方案不用 B 方案。  
這週它又建議 B 方案——因為它不記得上次的討論。

更糟的是：你自己也不一定記得為什麼選了 A。

### 3. 進度斷裂

一個跨 session 的任務（重構、遷移、多步驟部署），每次都要手動交接：
- 「上次做到 Step 3」
- 「這些檔案已經改了，那些還沒」
- 「有個 bug 卡住了，錯誤訊息是...」

人腦不該做這種工作。

---

## 解法：讓 Claude 自己管理 Session 記憶

核心概念很簡單：

```
Session 開始 → 讀上次的紀錄 → 接續工作
Session 結束 → 寫這次的紀錄 → 下次能接
```

具體來說，你需要三個動作：

| 指令 | 做什麼 | 何時用 |
|------|--------|--------|
| `/start` | 讀記錄，顯示「上次做到哪」 | 每次開 session |
| `/save` | 存當前進度（中途檢查點） | 做完一個里程碑 |
| `/wrap-up` | 完整收尾 + 存檔 | 要關掉 session 時 |

---

## 兩種實作方式

### 方式 A：本機 SQLite（零依賴，3 分鐘設定）

最簡單的起步方式。所有記錄存在你電腦上的一個 SQLite 檔案裡。

**優點**：
- 不需要任何伺服器或外部服務
- 設定只要 3 分鐘
- 資料完全在你的電腦上
- 可以用 SQL 查詢歷史

**限制**：
- 只在一台電腦上有效
- 沒有語意搜尋（只能 keyword match）
- 跨裝置不同步

**設定方式**：  
→ [macOS 設定指南](guides/session-skills-macos.md)  
→ [Windows 設定指南](guides/session-skills-windows.md)

### 方式 B：memhall 伺服器（進階，支援語意搜尋）

部署一個輕量 API 伺服器，提供：

- **Hybrid 搜尋**（BM25 + 向量語意）— 不只 keyword，能找到「意思接近」的記錄
- **多裝置同步** — 辦公室和家裡的 Claude 共享記憶
- **Namespace 隔離** — 不同專案的記憶互不干擾
- **Baton 機制** — 追蹤跨 session 的 open loops、follow-ups、決策紀錄
- **MCP 整合** — Claude Code 原生支援，不需要 skill 就能用

**適合場景**：
- 同時在多台電腦上用 Claude
- 專案多、需要跨專案搜尋歷史決策
- 想讓多人共享記憶（團隊版 team-memhall）
- 需要追蹤「上次說要做但還沒做的事」（baton / open loops）

**部署方式**：  
→ 見 [deploy.md](deploy.md) 或 [README](../README.md)

---

## 實際使用場景

### 場景 1：接續昨天的重構

```
你：/start

Claude：
## Session 開場 Summary
**上次進度**（2026-07-07 wrap-up）：
重構 auth 模組，已完成 token refresh 邏輯，
剩下 middleware 整合還沒做。卡在 CORS 設定。

**下一步**：先解決 CORS，再整合 middleware。

要接續嗎？
```

不用花 10 分鐘交代背景——Claude 自己讀到了。

### 場景 2：避免重複決策

```
你：我想用 Redis 來做 cache

Claude：（讀到 session 記錄）
注意：2026-06-20 的 session 記錄顯示，
你們當時評估過 Redis vs 本機 LRU cache，
最後選了 LRU cache，理由是「這個服務單機部署，
Redis 增加維運成本但沒有跨節點需求」。

現在的需求有變嗎？
```

### 場景 3：收工時自動整理

```
你：先這樣，我去吃飯

Claude：
## Session 摘要
- **完成**: 修復 #117 附件上傳，部署到 trac-server
- **未完成**: API 文件更新
- **下一步**: 更新 MCP-API.html，加入 attachment endpoints
- **關鍵決策**: 用 Trac 1.6 SHA1 hashed path 而非自訂路徑
- **寫入紀錄**: SQLite ✓ | job-memo ✓
```

下次 `/start` 就能看到這些。

---

## 從 SQLite 升級到 memhall

如果你一開始用 SQLite，後來想要語意搜尋或多裝置同步，可以無痛升級：

1. 部署 memhall 伺服器（Docker 一行搞定）
2. 把 SQLite 資料匯入 memhall（`scripts/import-sqlite.py`）
3. 把 skill 裡的 SQLite 呼叫改成 memhall API 呼叫

記憶的格式是相容的——slug、content、tags 的結構不變。

---

## 開始使用

**最快的方式**：花 3 分鐘設定 SQLite 版。

- macOS → [session-skills-macos.md](guides/session-skills-macos.md)
- Windows → [session-skills-windows.md](guides/session-skills-windows.md)

**想要更完整的記憶系統**：部署 memhall。

- 部署指南 → [deploy.md](deploy.md)
- API 文件 → [api.md](api.md)
- 設計理念 → [design.md](design.md)
