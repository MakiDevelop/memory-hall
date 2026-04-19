我建議的最小 Dockerfile 修法是：不要再對 `sqlite-vec==0.1.6` 做 `--no-binary` 重裝；那條路不會過，因為 `0.1.6` 在 PyPI 沒有 sdist。實際可過的版本是把 image 內的 `sqlite-vec` 升到較新的 Linux ARM64 wheel（我建議直接 `0.1.9`，並同步更新 `pyproject.toml` + `uv.lock`），然後在 Docker build 內加一段 `sqlite_vec.load()` smoke test，讓壞 wheel 在 build 階段直接 fail。

## 我建議的 Dockerfile

前提：`pyproject.toml` / `uv.lock` 先把 `sqlite-vec` 從 `0.1.6` bump 到 `0.1.9`。

```dockerfile
# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ollama 2>/dev/null || \
    uv sync --no-dev --extra ollama

RUN /app/.venv/bin/python - <<'PY'
import sqlite3, sqlite_vec
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
print(conn.execute("select vec_version()").fetchone()[0])
PY
```

重點只有兩件事：

- 刪掉現在這條失敗的命令：
  `uv pip install --python /app/.venv/bin/python --reinstall --no-binary sqlite-vec 'sqlite-vec==0.1.6'`
- 用 build-time smoke test 取代「碰運氣 build 完再進容器才知道壞掉」。

如果 Maki 堅持「這一個 commit 只改 Dockerfile，不先動 dependency pin」，那次佳方案是只在 image 裡 override：

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --reinstall "sqlite-vec==0.1.9"
```

但我不推薦，因為 image 版本會和 repo pin 漂移。

## Root Cause

不是「PyPI 只有 x86 wheel」；那個判斷是錯的。比較接近事實的是：

1. `sqlite-vec==0.1.6` 的 **PyPI 確實有** Linux ARM64 wheel。
2. 但 `0.1.6` 的 Linux ARM64 binary artifact 線上有公開壞檔紀錄。
3. 你現在 Dockerfile 想改走 source build，也行不通，因為 `0.1.6` 沒有 sdist。

所以真正卡住的是：**`0.1.6` 這版在 Linux ARM64 上不值得再信任，Dockerfile 繞再多都只是賭 artifact。**

## 我實際查到的事實

### 1. `pip download` 在這個 sandbox 內無法直接驗證

我實跑了：

```bash
python -m pip download sqlite-vec==0.1.6 \
  --platform manylinux2014_aarch64 \
  --only-binary=:all: \
  --dest /tmp/sqlite-vec-pip-test
```

結果不是「找不到 ARM64 wheel」，而是 sandbox 連不到 PyPI：

```text
Failed to establish a new connection: [Errno 8] nodename nor servname provided, or not known
ERROR: Could not find a version that satisfies the requirement sqlite-vec==0.1.6
```

所以這一項我只能回報：**已實測，但 sandbox 無對外 PyPI 網路，不能用本機下載結果當證據。**

### 2. PyPI 的 `0.1.6` 不是只有 x86

repo 內的 [`uv.lock`](../../uv.lock) 已經鎖到了 `sqlite-vec==0.1.6` 的 PyPI wheel 清單。裡面明確存在：

- `sqlite_vec-0.1.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl`
- `sha256:7b0519d9cd96164cd2e08e8eed225197f9cd2f0be82cb04567692a0a4be02da3`

同一段也列出：

- macOS x86_64
- macOS arm64
- manylinux x86_64
- win_amd64

所以「PyPI wheel 真的是 x86」這條可以直接排除。

### 3. GitHub releases 的 Linux ARM64 asset 確實存在

`asg017/sqlite-vec` 的 `v0.1.6` release 頁面，以及 SourceForge 的 GitHub mirror，都列出：

- `sqlite-vec-0.1.6-loadable-linux-aarch64.tar.gz`
- `sqlite-vec-0.1.6-static-linux-aarch64.tar.gz`

這證明 upstream 在 release 層確實有出 Linux ARM64 artifact，不是缺平台。

來源：

- GitHub release: `https://github.com/asg017/sqlite-vec/releases/tag/v0.1.6`
- SourceForge mirror: `https://sourceforge.net/projects/sqlite-vec.mirror/files/v0.1.6/`

### 4. upstream 已經有人踩過同一坑

GitHub issue #251 標題就是：

- `vec0.so in sqlite-vec-0.1.6-loadable-linux-aarch64.tar.gz is ELF 32.`

Issue body 直接寫：

> I download `sqlite-vec-0.1.6-loadable-linux-aarch64.tar.gz`. But use file `vec0.so`, it seems it is arm 32

來源：

- `https://github.com/asg017/sqlite-vec/issues/251`

### 5. memory-hall 自己的 v0.1 benchmark 也跟這件事對上

repo 內基準文件已記錄：

- `image's sqlite-vec wheel was ELFCLASS32 -> vec0 didn't load`

來源：

- [`docs/benchmarks/results-2026-04-18.md`](../../docs/benchmarks/results-2026-04-18.md)

這和 issue #251 的方向一致，表示不是你們的 Dockerfile 幻覺，也不是 `buildx` 把 x86 wheel 裝錯平台那麼單純。

## 我對根因的結論

我的判讀是：

- `0.1.6` 的 Linux ARM64 artifact 線有問題。
- `--no-binary` 不是解法，因為沒有 sdist。
- 「繼續 pin 在 `0.1.6`，只修 Dockerfile」這件事本身就是不穩定策略。

我沒辦法在這個 sandbox 裡重新把 PyPI wheel 抓下來再做 `file` 驗證，所以我不能 100% 證明「PyPI 上那顆 ARM64 wheel 與 GitHub 的壞 tarball 是同一顆 binary」。但對 engineering decision 來說，這已經不重要了：**`0.1.6` 在 Linux ARM64 上有足夠多的壞訊號，不值得繼續賭。**

## 如果你真的要 fetch-from-github

只有在「PyPI mirror 壞掉，或你不信任 PyPI wheel」時，我才會走這條。

方案是：

1. 仍然保留 `pip install sqlite-vec`，因為 `memory-hall` 需要 Python module 的 `sqlite_vec.load()` 和 `sqlite_vec.serialize_float32()`。
2. 只在 `linux/arm64` image 裡，從 GitHub releases 抓較新版本的 `loadable-linux-aarch64.tar.gz`。
3. 同步抓該 release 的 `checksums.txt`，先驗 `sha256sum -c`。
4. 解壓出 `vec0.so`。
5. 用 Python 找出 `sqlite_vec` package 目錄，把 wheel 內附的 Linux extension binary 替換掉。
6. 立刻跑上面那段 smoke test。

這條能 work，但我仍然認為 **直接 bump 到較新版本，刪掉 Dockerfile override，才是比較乾淨的 commit。**

## 建議 commit 長相

我建議不要做 Dockerfile-only drift commit，而是做這個：

- commit message: `build: 升級 sqlite-vec 到 0.1.9 並在 image build 驗證 vec0`

內容：

1. `pyproject.toml`
   把 `sqlite-vec==0.1.6` 改成 `sqlite-vec==0.1.9`
2. `uv.lock`
   重新鎖檔
3. `Dockerfile`
   刪掉 `--no-binary sqlite-vec==0.1.6` 那行
4. `Dockerfile`
   加入 `sqlite_vec.load()` smoke test

如果真的只能先救火一個檔，那次佳 commit 是：

- commit message: `build: 在 ARM64 image 內 override sqlite-vec 並驗證 vec0`

但我會把它視為 temporary hotfix，不是最終解。
