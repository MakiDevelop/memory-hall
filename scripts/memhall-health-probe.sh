#!/usr/bin/env bash
# memhall primary health probe — lowest-cost monitor (S1-2)
#
# Intended host: mini2 itself (local curl) or a always-on VPS/mini that can reach Tailscale.
# On failure: append log + optional Telegram via notify script if present.
#
# Cron example (on mini2):
#   */5 * * * * /Users/maki/GitHub/memory-hall/scripts/memhall-health-probe.sh
#
# Env:
#   MH_URL          default http://127.0.0.1:9100  (on mini2) or http://100.89.41.50:9100
#   MH_API_TOKEN    or file ~/.config/memhall/token
#   MH_HEALTH_LOG   default ~/logs/memhall-health.log
#   MH_NOTIFY_CMD   optional command receiving failure message on stdin

set -euo pipefail

MH_URL="${MH_URL:-http://127.0.0.1:9100}"
TOKEN="${MH_API_TOKEN:-}"
if [[ -z "$TOKEN" && -f "${HOME}/.config/memhall/token" ]]; then
  TOKEN="$(cat "${HOME}/.config/memhall/token")"
fi
LOG="${MH_HEALTH_LOG:-$HOME/logs/memhall-health.log}"
mkdir -p "$(dirname "$LOG")"

ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
code="000"
if [[ -n "$TOKEN" ]]; then
  code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN}" \
    "${MH_URL}/v1/memory?limit=1" || true)"
else
  code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
    "${MH_URL}/v1/memory?limit=1" || true)"
fi

if [[ "$code" == "200" || "$code" == "401" || "$code" == "403" ]]; then
  # 401/403 means process is up (auth rejected) — still "alive"
  echo "${ts} OK code=${code} url=${MH_URL}" >>"$LOG"
  exit 0
fi

msg="${ts} FAIL code=${code} url=${MH_URL} (memhall primary unhealthy)"
echo "$msg" >>"$LOG"

if [[ -n "${MH_NOTIFY_CMD:-}" ]]; then
  echo "$msg" | bash -c "$MH_NOTIFY_CMD" || true
elif command -v telegram-send >/dev/null 2>&1; then
  telegram-send "$msg" || true
fi

exit 1
