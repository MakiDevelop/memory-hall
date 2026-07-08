# Session 管理技能包 — Windows 設定指南

> 適用：Windows + Claude Desktop Code  
> 功能：`/start`（開場接續）、`/save`（中途存檔）、`/wrap-up`（收工）  
> 儲存：本機 SQLite，不需要外部服務

---

## 一、快速設定（3 分鐘）

### 1. 建立目錄

打開 PowerShell，貼上：

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills\session-start"
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills\session-save"
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills\session-wrap-up"
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\job-memo"
```

### 2. 初始化 SQLite

```powershell
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime(\"now\", \"localtime\")),
    slug TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT \"episode\",
    content TEXT NOT NULL,
    tags TEXT DEFAULT \"\"
)''')
conn.commit()
conn.close()
print(f'DB created: {db}')
"
```

> **注意**：Windows 用 `python`（不是 `python3`）。如果出現 `python is not recognized`，請先安裝 Python：https://www.python.org/downloads/ ，安裝時勾選 "Add to PATH"。

### 3. 放入 Skill 檔案

把下面三個段落的內容，分別存成對應的檔案。  
路徑中的 `%USERPROFILE%` 通常是 `C:\Users\你的帳號`。

---

## 二、Skill 檔案

### 檔案 1：`%USERPROFILE%\.claude\skills\session-start\SKILL.md`

```markdown
# /start — Session 開場

Session 開始時，從本機 SQLite 拉最近的工作記錄，產出接續摘要。

## 流程

### Step 1：讀取最近 5 筆 session 記錄

用 python 查詢 SQLite：

\```bash
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
rows = conn.execute(
    'SELECT created_at, slug, type, content FROM sessions ORDER BY id DESC LIMIT 5'
).fetchall()
conn.close()
if not rows:
    print('（尚無歷史記錄，這是第一次使用）')
else:
    for r in rows:
        print(f'[{r[0]}] ({r[2]}) {r[1]}')
        for line in r[3].split(chr(10))[:5]:
            print(f'  {line}')
        print()
"
\```

### Step 2：讀取最近的 job-memo

\```bash
dir /b /o-d "%USERPROFILE%\.claude\job-memo\*.md" 2>nul | head -2
\```

如果有檔案，用 Read 工具讀取最新的 1-2 份。  
job-memo 路徑：`%USERPROFILE%\.claude\job-memo\`

### Step 3：Git 狀態（如果在 repo 內）

\```bash
git log --oneline -5 2>nul
git status -sb 2>nul
\```

### Step 4：輸出摘要

整理以上資訊，用以下格式輸出：

\```
## Session 開場 Summary

**上次進度**：
{最新一筆 session 記錄的摘要}

**最近 sessions**：
- [日期] slug — 第一行摘要
- ...

**最近 commits**：
{git log 摘要}

---
要接續上次的工作嗎？還是新任務？
\```

如果沒有任何歷史記錄，就說「這是第一個 session，請告訴我要做什麼」。
```

---

### 檔案 2：`%USERPROFILE%\.claude\skills\session-save\SKILL.md`

```markdown
# /save — 中途存檔

把當前進度存入本機 SQLite，不中斷工作。

## 流程

### Step 1：整理當前進度

回顧本次 session 到目前為止：
- 做了什麼（具體，不要模糊）
- 改了哪些檔案
- 關鍵決策
- 下一步

### Step 2：產生 slug

從任務主題產生一個簡短的 slug（英文小寫 + 連字號，如 `fix-login-bug`）。

### Step 3：寫入 SQLite

\```bash
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
conn.execute(
    'INSERT INTO sessions (slug, type, content, tags) VALUES (?, ?, ?, ?)',
    (
        'SLUG_HERE',
        'checkpoint',
        '''CONTENT_HERE''',
        'TAG1, TAG2, TAG3'
    )
)
conn.commit()
conn.close()
print('checkpoint saved')
"
\```

把 `SLUG_HERE`、`CONTENT_HERE`、`TAG1...` 替換為實際內容。

content 至少包含：
1. 做了什麼
2. 改了哪些檔案
3. 下一步

tags 至少 3 個關鍵字（方便未來搜尋）。

### Step 4：簡短回報

告訴使用者「已存進度」，一句話說存了什麼。不要長篇報告。
```

---

### 檔案 3：`%USERPROFILE%\.claude\skills\session-wrap-up\SKILL.md`

```markdown
# /wrap-up — 收工

Session 結束前的完整收尾流程。

## 觸發時機

使用者說以下任何一句時啟動：
- 「收工」「下班」「結束」「先這樣」「今天到這」
- 「wrap-up」「收尾」
- 明顯要離開的語氣（「我去吃飯」「明天再說」）

## 流程

### Step 1：整理本次 session 完整內容

回顧整個 session，整理：
1. **背景** — 為什麼做這件事
2. **完成什麼** — 具體產出
3. **關鍵決策** — 為什麼選 A 不選 B
4. **改了哪些檔案** — 列出路徑
5. **下一步** — 接手者第一件事做什麼

### Step 2：寫入 SQLite

\```bash
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
conn.execute(
    'INSERT INTO sessions (slug, type, content, tags) VALUES (?, ?, ?, ?)',
    (
        'SLUG_HERE',
        'wrap-up',
        '''CONTENT_HERE''',
        'TAG1, TAG2, TAG3, TAG4, TAG5'
    )
)
conn.commit()
conn.close()
print('wrap-up saved')
"
\```

content 必須 ≥ 200 字，tags ≥ 5 個關鍵字。

### Step 3：寫入 Job Memo

用 Write 工具寫入 `%USERPROFILE%\.claude\job-memo\YYYY-MM-DD-SLUG.md`：

\```markdown
# TASK_NAME — DATE

## 今天做了什麼
- ...

## 改了哪些檔案
- ...

## 未完成 / 下次接續
- ...

## 注意事項
- ...
\```

### Step 4：輸出 Session 摘要

在對話中輸出：

\```
## Session 摘要
- **完成**: ...
- **未完成**: ...
- **下一步**: ...
- **關鍵決策**: ...
- **寫入紀錄**: SQLite ✓ | job-memo ✓
\```

## 禁止

- 使用者要走時，不做 wrap-up 就結束 → 必須提醒
- 寫模糊內容（「更新了設定」「做了一些修改」）→ 不合格，要具體
```

---

## 三、使用方式

在 Claude Desktop Code 的對話中直接輸入：

| 指令 | 用途 | 時機 |
|------|------|------|
| `/start` | 開場，看上次做到哪 | 每次開新 session |
| `/save` | 中途存檔 | 完成一個里程碑時 |
| `/wrap-up` | 收工 | 要離開時 |

## 四、查詢歷史記錄

想手動看自己的歷史，在 PowerShell 跑：

```powershell
# 看最近 10 筆
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
for r in conn.execute('SELECT created_at, type, slug FROM sessions ORDER BY id DESC LIMIT 10').fetchall():
    print(f'[{r[0]}] ({r[1]}) {r[2]}')
conn.close()
"

# 搜尋關鍵字（把「關鍵字」換成你要找的）
python -c "
import sqlite3, os
db = os.path.join(os.environ['USERPROFILE'], '.claude', 'sessions.db')
conn = sqlite3.connect(db)
kw = '關鍵字'
for r in conn.execute('SELECT created_at, slug, content FROM sessions WHERE content LIKE ? OR tags LIKE ? ORDER BY id DESC LIMIT 5', (f'%{kw}%', f'%{kw}%')).fetchall():
    print(f'[{r[0]}] {r[1]}')
    print(r[2][:200])
    print()
conn.close()
"
```

## 五、疑難排解

| 問題 | 解法 |
|------|------|
| `python is not recognized` | 安裝 Python，勾選 "Add to PATH"，重開 PowerShell |
| `/start` 說找不到 skill | 確認 `%USERPROFILE%\.claude\skills\session-start\SKILL.md` 存在 |
| SQLite 錯誤 | 跑一次 Step 2 的初始化指令 |
| 中文亂碼 | 確認 SKILL.md 用 UTF-8 編碼存檔（VS Code 右下角可切換） |
