#!/usr/bin/env bash
# 向当前用户的 crontab 注册一条「增量同步」任务（Linux / macOS）。
# 用法（在仓库根目录执行）：
#   ./batchfiles/install-scheduled-sync-unix.sh
#   ./batchfiles/install-scheduled-sync-unix.sh hourly 4
#   ./batchfiles/install-scheduled-sync-unix.sh daily 22:00
#   ./batchfiles/install-scheduled-sync-unix.sh --remove
#
# 也可：SYNC_MODE=daily DAILY_AT=07:30 ./batchfiles/install-scheduled-sync-unix.sh
# 或通过：uv run onelap2strava auto-sync install

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="#onelap2strava-scheduled-sync"

# 日志文件；设为空字符串则不在 crontab 里重定向（不推荐）
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
  echo "[ok] 已移除带标记 $TAG 的 crontab 条目。"
  exit 0
fi

# 命令行优先：hourly N | daily HH:MM；否则沿用环境变量或下方默认
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
  echo "[error] 未找到 uv（PATH 中无 uv）。请先安装 uv 后再运行本脚本。" >&2
  exit 1
fi

if [[ "$SYNC_MODE" != "hourly" && "$SYNC_MODE" != "daily" ]]; then
  echo "[error] SYNC_MODE 必须是 hourly 或 daily" >&2
  exit 1
fi

if [[ "$SYNC_MODE" == "hourly" ]]; then
  if ! [[ "$HOURLY_INTERVAL" =~ ^[0-9]+$ ]] || [[ "$HOURLY_INTERVAL" -lt 1 || "$HOURLY_INTERVAL" -gt 23 ]]; then
    echo "[error] HOURLY_INTERVAL 须为 1–23 的整数" >&2
    exit 1
  fi
  CRON_EXPR="0 */${HOURLY_INTERVAL} * * *"
else
  if [[ "$DAILY_AT" != *:* ]]; then
    echo "[error] DAILY_AT 须为 HH:MM（24 小时制），例如 22:00 或 7:30" >&2
    exit 1
  fi
  IFS=: read -r HOUR MIN <<< "$DAILY_AT"
  HOUR=$((10#${HOUR:-0}))
  MIN=$((10#${MIN:-0}))
  if (( HOUR < 0 || HOUR > 23 || MIN < 0 || MIN > 59 )); then
    echo "[error] DAILY_AT 小时须在 0–23、分钟须在 0–59" >&2
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
echo "[ok] crontab 已更新。当前 crontab："
crontab -l
echo ""
echo "移除：uv run onelap2strava auto-sync uninstall"
