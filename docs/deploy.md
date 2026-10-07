# Deployment

This doc covers how the author's own home lab is wired. Adapt paths and hosts for yours.

## Topology

```
┌─────────────────────┐        ┌──────────────────────┐
│  Primary host        │        │  Backup host          │
│  (serves writes/     │◀──────│  (cold standby)       │
│   reads)             │ rsync  │  container: stopped   │
│  :9100 exposed       │ /5min  │  data: synced         │
└──────────────────────┘        └──────────────────────┘
           │
           ↓ MH_OLLAMA_BASE_URL
┌──────────────────────┐
│  Embedder host       │
│  (Ollama + bge-m3,   │
│   keep_alive=-1)     │
└──────────────────────┘
```

Primary and backup run the **same** `memory-hall:0.1.0` image with **different** runtime state. The backup is intentionally stopped; on failover you `docker start memory-hall` on the backup and switch your DNS / traffic.

## Primary host

```bash
docker load < memory-hall-0.1.0.tar.gz   # or docker pull if using a registry
mkdir -p ~/data/memory-hall
docker run -d \
    --name memory-hall \
    --restart unless-stopped \
    -p 9100:9000 \
    -e MH_OLLAMA_BASE_URL=http://<embedder-host>:11434 \
    -v ~/data/memory-hall:/data \
    memory-hall:0.1.0
```

Bind-mount (not named volume) is intentional: the backup host's rsync needs direct file access.

## Backup host (cold standby)

```bash
# Install the image, but don't run it yet
docker load < memory-hall-0.1.0.tar.gz
docker create \
    --name memory-hall \
    --restart unless-stopped \
    -p 9100:9000 \
    -e MH_OLLAMA_BASE_URL=http://<embedder-host>:11434 \
    -v ~/data/memory-hall:/data \
    memory-hall:0.1.0
# docker create leaves it in "Created" state, not running. docker start when failing over.

# Install backup script + cron
cp deploy/memhall-backup.sh ~/bin/memhall-backup.sh
chmod +x ~/bin/memhall-backup.sh
# Edit MEMHALL_SRC_HOST if default primary IP isn't yours
(crontab -l 2>/dev/null; echo "*/5 * * * * /Users/maki/bin/memhall-backup.sh") | crontab -
```

Backup script preserves the WAL triplet (`.sqlite3`, `-shm`, `-wal`). SQLite can read the synced state consistently as long as all three files are copied together.

## Failover

When primary is down:

```bash
# On backup host
docker start memory-hall
curl http://localhost:9100/v1/health
```

Then switch your callers (agent base URLs) to point at the backup. There is no automatic DNS switch.

## Health monitoring

The image has a built-in `HEALTHCHECK` on `/v1/health` (30s interval). External uptime checks can poll the same path.

## Upgrade path (rolling)

1. Build new image on dev machine
2. `docker save` → scp to primary
3. `docker load` on primary
4. `docker stop && docker rm memory-hall && docker run ... memory-hall:X.Y.Z`
5. Verify `/v1/health` + a test write/search
6. Repeat for backup host (backup container stays stopped, image just replaced)

For production with zero-downtime needs, see v0.2 roadmap — not supported in v0.1.

## Known constraints (v0.1)

- **Single writer assumption**: one uvicorn worker per container. Don't scale horizontally.
- **Cold standby only**: running primary and backup simultaneously will split-brain the writes.
- **No automatic failover**: manual DNS / config switch.
- **No encryption at rest**: SQLite files are plaintext. Use disk-level encryption (FileVault / LUKS) if your data warrants it.

## Deploy footguns (learned the hard way)

See [`docs/operations/incident-2026-04-20-embed-queue.md`](operations/incident-2026-04-20-embed-queue.md) for the full story. Short version:

### Don't embed through a shared Ollama

If the same Ollama instance serves large LLM clients, `bge-m3` will starve. Either point memory-hall at a dedicated embed service (`MH_EMBEDDER_KIND=http` + `MH_EMBED_BASE_URL=...`) or keep Ollama exclusive to embeddings. See [ADR 0006](adr/0006-http-embedder-embed-queue-isolation.md).

### Back up before `docker compose up --force-recreate`

If your existing deployment was created with plain `docker run`, compose may replace your data volume on recreate. Always snapshot first:

```bash
docker run --rm -v memory-hall_mh-data:/backup alpine \
    tar czf - /backup > memhall-backup-$(date +%F).tar.gz
```

Or — and this is the pattern this doc has recommended since v0.1 — use a **bind mount** (`-v ~/data/memory-hall:/data`) instead of a named volume. Bind mounts are transparent, trivially backed up via `rsync`, and compose cannot silently swap them.

As of post-0.2, `docker-compose.yml` matches this doc: the mount is a bind driven by `MEMHALL_DATA_DIR` (default `./mh-data` inside the repo). For production, set it to an absolute path (e.g. `MEMHALL_DATA_DIR=~/data/memory-hall`).

If you have an existing deployment still on a named `mh-data` volume, migrate before recreating:

```bash
# 1. Stop the container
docker compose stop memory-hall

# 2. Copy named-volume contents to the target bind-mount host path
mkdir -p ~/data/memory-hall
docker run --rm \
    -v memory-hall_mh-data:/src \
    -v ~/data/memory-hall:/dst \
    alpine sh -c 'cp -a /src/. /dst/'

# 3. Export the env var and bring it back up (compose now binds ~/data/memory-hall)
export MEMHALL_DATA_DIR=~/data/memory-hall
docker compose up -d memory-hall

# 4. Verify entry count matches before you remove the old named volume
curl -s http://localhost:9100/v1/memory | jq '.total'
# Only after this matches expectation:
# docker volume rm memory-hall_mh-data
```

### macOS-specific: keychain must be unlocked for `docker compose build`

Docker Desktop's credential helper requires GUI keychain access. `ssh` into a Mac to build and you'll see `keychain cannot be accessed because the current session does not allow user interaction`. Run `security -v unlock-keychain ~/Library/Keychains/login.keychain-db` in an interactive session first, or build elsewhere and `docker save | docker load` across.

### Port alignment

This repo's `docker-compose.yml` exposes `9100:9000` (host:container). If your existing deployment was started with a different host port, callers coded against the old port will break on the first `force-recreate`. Grep your agent stack for the literal port number before redeploying.

## Ordered embedding failover

Embedding service failover is independent of memory-hall storage: the memory-hall
backup remains cold standby and requires manual promotion.

Example environment (deployment requires operator approval):

```dotenv
MH_EMBEDDER_KIND=http
MH_EMBED_BASE_URL=
MH_EMBED_BASE_URLS=http://100.110.14.65:8790,http://host.docker.internal:8790,http://100.97.228.45:8790
MH_EMBED_MODEL=BAAI/bge-m3
MH_EMBED_DIM=1024
MH_VECTOR_DIM=1024
MH_EMBED_CONNECT_TIMEOUT_S=2
MH_EMBED_TIMEOUT_S=8
MH_EMBED_COOLDOWN_S=60
MH_EMBED_MISMATCH_RECHECK_S=600
```

The list order is DGX Spark → mini2 → mini1. These are configuration examples,
not evidence that the services are currently deployed or reachable. Each target
must serve both `/health` and `/embed`; installing mini1's embed service is a
separate operation. URLs must use HTTP(S), without embedded credentials, query
parameters or fragments. Empty list items are ignored. Ollama ignores the URL settings.

`MH_EMBED_BASE_URL` alone becomes a list of one and uses the same consistency
checks. Both URL settings non-empty fail at startup. `GET /health` must return
an object with string `model` and integer `dimension`, matching `MH_EMBED_MODEL`
and `MH_EMBED_DIM` / `MH_VECTOR_DIM`. Missing/invalid metadata cools down the node;
explicit model/dimension mismatch excludes it until a successful lazy health recheck,
with a separate `MH_EMBED_MISMATCH_RECHECK_S` interval (default 600 seconds). Every embed
response is also checked for dimension, vector count and finite numeric values.

Connection errors, timeouts, HTTP 5xx and invalid payloads trigger a 60-second
cooldown (configurable). HTTP 4xx propagate without failover. No new background
worker is introduced: the next embedding attempt after cooldown rechecks health,
starting from the first URL. Existing runtime health probes also exercise this path.

Timeout views share a small state lock and per-backend health probe locks.
Embedding HTTP calls hold no locks and run concurrently, including write/search.
Requests wait at most 50 ms (within their remaining budget) for an in-flight probe,
then skip that backend if still busy. Only a matching health probe permits use.
Concurrent stale results cannot clear a newer failure or mismatch.

`MH_EMBED_TIMEOUT_S` is the per-backend HTTP read/write/pool timeout; connect uses
`MH_EMBED_CONNECT_TIMEOUT_S`. The write/reindex outer budget is the sum over
backends of `2 * (connect + 3 * read)` seconds, covering health + embed and HTTP
phases. This is a safety cap, not a latency target. All disconnected nodes usually
consume connect timeouts only; read stalls can take longer. Avoid retaining a
120-second read timeout unless that latency is intended. Search and health retain
their existing total budgets (`MH_SEARCH_EMBED_TIMEOUT_S`,
`MH_HEALTH_EMBED_TIMEOUT_S`), so they may expire before all nodes are tried.
Synchronous HTTP work already in progress cannot be cancelled by asyncio's outer
timeout; HTTP phase timeouts bound it, and no new attempt begins after the deadline.

`/v1/health` adds `embed_backends: [{"url": "...", "state": "..."}]` and
`last_embed_backend` (null before success; includes health/search embeddings).
States are `healthy`, `cooling_down`, `mismatch`. Not-yet-probed nodes initially
show `cooling_down` but are immediately eligible. Snapshots do not probe the
network; aggregate health retains its existing cache. Backend state transitions
log once per change without exception payloads. A healthy fallback keeps overall
health OK even if the primary is cooling down.

If every backend fails, the final backend's original exception class is raised
(and retained during cooldown), with sanitized error text. The write is already
in SQLite: HTTP 202, `embedded=false`, `sync_status=pending`, error/attempt time
recorded, attempt count incremented once per failed write/reindex entry attempt.
The fifth failed attempt marks it `failed`; pending-only retries then skip it.
Lexical search remains available; no invalid vectors are written. Existing
startup/periodic pending reindex behavior is unchanged.
