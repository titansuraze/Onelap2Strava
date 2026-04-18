#!/usr/bin/env bash
# 一次性执行增量同步；工作目录为仓库根目录（本脚本位于 batchfiles/ 下）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "[error] 未找到 uv，请先安装 uv 或将其加入 PATH。" >&2
  exit 1
fi
exec uv run onelap2strava sync --incremental
