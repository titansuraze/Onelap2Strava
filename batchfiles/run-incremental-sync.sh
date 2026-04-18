#!/usr/bin/env bash
# One-shot incremental sync; cwd is repo root (this script is under batchfiles/).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "[error] uv not found. Install uv or add it to PATH." >&2
  exit 1
fi
exec uv run onelap2strava sync --incremental
