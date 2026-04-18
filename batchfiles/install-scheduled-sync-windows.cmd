@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if /i "%~1"=="uninstall" goto :uninstall
if /i "%~1"=="remove" goto :uninstall

REM =============================================================================
REM No args: use defaults below.
REM Override: hourly N  or  daily HH:mm
REM Or use: uv run onelap2strava auto-sync install
REM =============================================================================
set "TASK_NAME=Onelap2StravaIncrementalSync"
set "SYNC_MODE=hourly"
set "HOURLY_INTERVAL=4"
set "DAILY_TIME=22:00"

if /i "%~1"=="hourly" (
  set "SYNC_MODE=hourly"
  if not "%~2"=="" set "HOURLY_INTERVAL=%~2"
  goto :ready
)
if /i "%~1"=="daily" (
  set "SYNC_MODE=daily"
  if not "%~2"=="" set "DAILY_TIME=%~2"
  goto :ready
)

:ready
set "RUNNER=%~dp0run-incremental-sync.cmd"
if not exist "%RUNNER%" (
  echo [error] run-incremental-sync.cmd not found next to this script. >&2
  exit /b 1
)

if /i not "%SYNC_MODE%"=="hourly" if /i not "%SYNC_MODE%"=="daily" (
  echo [error] SYNC_MODE must be hourly or daily. >&2
  exit /b 1
)

if /i "%SYNC_MODE%"=="hourly" (
  if "%HOURLY_INTERVAL%"=="" (
    echo [error] HOURLY_INTERVAL is not set. >&2
    exit /b 1
  )
  echo Creating scheduled task "%TASK_NAME%": every %HOURLY_INTERVAL% hours...
  schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc HOURLY /mo %HOURLY_INTERVAL% /f
  if errorlevel 1 (
    echo [error] schtasks failed. If access denied, run this script as Administrator. >&2
    exit /b 1
  )
) else (
  echo Creating scheduled task "%TASK_NAME%": daily at %DAILY_TIME%...
  schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc DAILY /st %DAILY_TIME% /f
  if errorlevel 1 (
    echo [error] schtasks failed. If access denied, run this script as Administrator. >&2
    exit /b 1
  )
)

echo [ok] Scheduled task registered. Query: schtasks /query /tn "%TASK_NAME%" /v /fo LIST
echo      Remove: uv run onelap2strava auto-sync uninstall
exit /b 0

:uninstall
set "TASK_NAME=Onelap2StravaIncrementalSync"
if not "%~2"=="" set "TASK_NAME=%~2"
echo Deleting scheduled task "%TASK_NAME%"...
schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 (
  echo [warn] Delete failed (task missing or Administrator required). >&2
  exit /b 1
)
echo [ok] Deleted.
exit /b 0
