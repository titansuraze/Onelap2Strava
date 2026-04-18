#!/usr/bin/env bash
# Install a cron job for incremental sync (Linux / macOS). Run from repo root, e.g.:
#   ./batchfiles/install-scheduled-sync-unix.sh
#   ./batchfiles/install-scheduled-sync-unix.sh hourly 4
#   ./batchfiles/install-scheduled-sync-unix.sh daily 22:00
#   ./batchfiles/install-scheduled-sync-unix.sh --remove
#
# Or: SYNC_MODE=daily DAILY_AT=07:30 ./batchfiles/install-scheduled-sync-unix.sh
# Or: uv run onelap2strava auto-sync install

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="#onelap2strava-scheduled-sync"

# Log file; empty string disables redirect in crontab (not recommended)
CRON_LOG="${CRON_LOG:-$ROOT/data/sync-cron.log}"

if [[ "${1:-}" == "--remove" ]] || [[ "${1:-}" == "--uninstall" ]]; then
  tmp="$(mktemp)"
  if crontab -l 2>/dev/null | awk -v tag="$TAG" '
    $0 == tag { skip = 1; next }
    skip { skip = 0; next }
    { print }
  ' >"$tmp"; then
    crontab "$tmp"
  fi
  rm -f "$tmp"
  echo "[ok] Removed crontab lines tagged with $TAG."
  exit 0
fi

# CLI args override: hourly N | daily HH:MM; else env or defaults below
if [[ "${1:-}" == "hourly" ]]; then
  SYNC_MODE=hourly
  HOURLY_INTERVAL="${2:-4}"
elif [[ "${1:-}" == "daily" ]]; then
  SYNC_MODE=daily
  DAILY_AT="${2:-22:00}"
else
  SYNC_MODE="${SYNC_MODE:-hourly}"
  HOURLY_INTERVAL="${HOURLY_INTERVAL:-4}"
  DAILY_AT="${DAILY_AT:-22:00}"
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "[error] uv not found in PATH. Install uv before running this script." >&2
  exit 1
fi

if [[ "$SYNC_MODE" != "hourly" && "$SYNC_MODE" != "daily" ]]; then
  echo "[error] SYNC_MODE must be hourly or daily." >&2
  exit 1
fi

if [[ "$SYNC_MODE" == "hourly" ]]; then
  if ! [[ "$HOURLY_INTERVAL" =~ ^[0-9]+$ ]] || [[ "$HOURLY_INTERVAL" -lt 1 || "$HOURLY_INTERVAL" -gt 23 ]]; then
    echo "[error] HOURLY_INTERVAL must be an integer from 1 to 23." >&2
    exit 1
  fi
  CRON_EXPR="0 */${HOURLY_INTERVAL} * * *"
else
  if [[ "$DAILY_AT" != *:* ]]; then
    echo "[error] DAILY_AT must be HH:MM (24h), e.g. 22:00 or 7:30." >&2
    exit 1
  fi
  IFS=: read -r HOUR MIN <<< "$DAILY_AT"
  HOUR=$((10#${HOUR:-0}))
  MIN=$((10#${MIN:-0}))
  if (( HOUR < 0 || HOUR > 23 || MIN < 0 || MIN > 59 )); then
    echo "[error] DAILY_AT hour must be 0-23 and minute 0-59." >&2
    exit 1
  fi
  CRON_EXPR="$MIN $HOUR * * *"
fi

if [[ -n "$CRON_LOG" ]]; then
  mkdir -p "$(dirname "$CRON_LOG")"
  CRON_JOB="cd '$ROOT' && '$UV_BIN' run onelap2strava sync --incremental >>'$CRON_LOG' 2>&1"
else
  CRON_JOB="cd '$ROOT' && '$UV_BIN' run onelap2strava sync --incremental"
fi

tmp="$(mktemp)"
if crontab -l 2>/dev/null | awk -v tag="$TAG" '
  $0 == tag { skip = 1; next }
  skip { skip = 0; next }
  { print }
' >"$tmp"; then
  :
fi
{
  cat "$tmp"
  echo "$TAG"
  echo "$CRON_EXPR $CRON_JOB"
} | crontab -

rm -f "$tmp"
echo "[ok] crontab updated. Current crontab:"
crontab -l
echo ""
echo "Remove: uv run onelap2strava auto-sync uninstall"
