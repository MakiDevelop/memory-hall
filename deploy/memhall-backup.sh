#!/bin/bash
# memhall cold-backup rsync
#
# Pulls the SQLite WAL triplet (.sqlite3 / -shm / -wal) from a primary host
# to this host's data dir. Run on the BACKUP host via cron.
#
# Assumes:
#   - Primary host runs memory-hall with a bind-mounted data dir
#   - Backup host has this script at ~/bin/memhall-backup.sh
#   - Backup host can SSH to primary (key-based, known_hosts populated)
#   - Backup host's memory-hall container is STOPPED while acting as cold standby
#     (if running, rsync overwriting an open SQLite will corrupt state)
#
# Cron example (backup host):
#   */5 * * * * /Users/maki/bin/memhall-backup.sh
#
# Edit SRC_HOST / SRC_DIR / DST for your topology.

set -euo pipefail

# Topology (2026-07-24 R3): primary = mini2, cold standby = mini1.
# This script runs ON the backup host and pulls FROM primary.
SRC_HOST="${MEMHALL_SRC_HOST:-100.89.41.50}"
SRC_DIR="${MEMHALL_SRC_DIR:-data/memory-hall/}"
DST="${MEMHALL_DST:-$HOME/data/memory-hall/}"
LOG="${MEMHALL_LOG:-$HOME/logs/memhall-backup.log}"

mkdir -p "$(dirname "$LOG")" "$DST"

{
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
  rsync -a --delete \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
    "${SRC_HOST}:${SRC_DIR}" "$DST" 2>&1
  echo "rc=$?"
} >> "$LOG" 2>&1
