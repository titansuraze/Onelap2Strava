#!/usr/bin/env bash
# 向当前用户的 crontab 注册一条「增量同步」任务（Linux / macOS）。
# 用法：
#   ./install-scheduled-sync-unix.sh           # 按下方变量安装/覆盖本仓库对应条目
#   ./install-scheduled-sync-unix.sh --remove  # 移除本脚本写入的 crontab 条目
#
# 也可在安装前通过环境变量覆盖（无需改文件）：
#   SYNC_MODE=daily DAILY_AT=07:30 ./install-scheduled-sync-unix.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TAG="#onelap2strava-scheduled-sync"

# =============================================================================
# 用户配置（可直接修改后执行本脚本）
# SYNC_MODE=hourly — 每 HOURLY_INTERVAL 小时一次（从 0 分起，与 cron 小时字段 */N 一致）
# SYNC_MODE=daily  — 每天在 DAILY_AT 执行（24 小时制 HH:MM）
# =============================================================================
SYNC_MODE="${SYNC_MODE:-hourly}"
HOURLY_INTERVAL="${HOURLY_INTERVAL:-4}"
DAILY_AT="${DAILY_AT:-22:00}"
# 日志文件；设为空字符串则不在 crontab 里重定向（不推荐）
CRON_LOG="${CRON_LOG:-$ROOT/data/sync-cron.log}"
# =============================================================================

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
echo "移除：$0 --remove"
