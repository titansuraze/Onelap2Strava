@echo off
setlocal
REM Working directory: repo root (this script lives under batchfiles/)
cd /d "%~dp0..\"
where uv >nul 2>&1
if errorlevel 1 (
  echo [error] uv not found. Install uv or add it to PATH. >&2
  exit /b 1
)
uv run onelap2strava sync --incremental
exit /b %ERRORLEVEL%
