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

### macOS-specific: keychain must be unlocked for `docker compose build`

Docker Desktop's credential helper requires GUI keychain access. `ssh` into a Mac to build and you'll see `keychain cannot be accessed because the current session does not allow user interaction`. Run `security -v unlock-keychain ~/Library/Keychains/login.keychain-db` in an interactive session first, or build elsewhere and `docker save | docker load` across.

### Port alignment

This repo's `docker-compose.yml` exposes `9100:9000` (host:container). If your existing deployment was started with a different host port, callers coded against the old port will break on the first `force-recreate`. Grep your agent stack for the literal port number before redeploying.
