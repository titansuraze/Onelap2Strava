@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 (
  echo [error] 未找到 uv，请先安装 uv 或将其加入 PATH。 >&2
  exit /b 1
)
uv run onelap2strava sync --incremental
exit /b %ERRORLEVEL%
