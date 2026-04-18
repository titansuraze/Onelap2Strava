#!/usr/bin/env bash
# 一次性执行增量同步；工作目录固定为脚本所在仓库根目录。
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo "[error] 未找到 uv，请先安装 uv 或将其加入 PATH。" >&2
  exit 1
fi
exec uv run onelap2strava sync --incremental
