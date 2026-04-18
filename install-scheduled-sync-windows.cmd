@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if /i "%~1"=="uninstall" goto :uninstall
if /i "%~1"=="remove" goto :uninstall

REM =============================================================================
REM 用户配置：按需修改后，在资源管理器中双击本文件，或在「以管理员身份运行」的
REM cmd 中执行：install-scheduled-sync-windows.cmd
REM
REM SYNC_MODE=hourly  — 每 HOURLY_INTERVAL 小时运行一次（整点起算，与任务计划「按间隔」一致）
REM SYNC_MODE=daily   — 每天在 DAILY_TIME 运行一次（24 小时制 HH:mm）
REM =============================================================================
set "TASK_NAME=Onelap2StravaIncrementalSync"
set "SYNC_MODE=hourly"
set "HOURLY_INTERVAL=4"
set "DAILY_TIME=22:00"
REM =============================================================================

set "RUNNER=%~dp0run-incremental-sync.cmd"
if not exist "%RUNNER%" (
  echo [error] 找不到同目录下的 run-incremental-sync.cmd >&2
  exit /b 1
)

if /i not "%SYNC_MODE%"=="hourly" if /i not "%SYNC_MODE%"=="daily" (
  echo [error] SYNC_MODE 必须是 hourly 或 daily >&2
  exit /b 1
)

if /i "%SYNC_MODE%"=="hourly" (
  if "%HOURLY_INTERVAL%"=="" (
    echo [error] HOURLY_INTERVAL 未设置 >&2
    exit /b 1
  )
  echo 正在创建计划任务 "%TASK_NAME%"：每 %HOURLY_INTERVAL% 小时执行一次...
  schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc HOURLY /mo %HOURLY_INTERVAL% /f
  if errorlevel 1 (
    echo [error] schtasks 失败。若提示权限不足，请右键「以管理员身份运行」本脚本。 >&2
    exit /b 1
  )
) else (
  echo 正在创建计划任务 "%TASK_NAME%"：每天在 %DAILY_TIME% 执行...
  schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc DAILY /st %DAILY_TIME% /f
  if errorlevel 1 (
    echo [error] schtasks 失败。若提示权限不足，请右键「以管理员身份运行」本脚本。 >&2
    exit /b 1
  )
)

echo [ok] 计划任务已注册。查看：schtasks /query /tn "%TASK_NAME%" /v /fo LIST
echo      卸载：install-scheduled-sync-windows.cmd uninstall
exit /b 0

:uninstall
set "TASK_NAME=Onelap2StravaIncrementalSync"
if not "%~2"=="" set "TASK_NAME=%~2"
echo 正在删除计划任务 "%TASK_NAME%"...
schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 (
  echo [warn] 删除失败（任务可能不存在或需要管理员权限）。 >&2
  exit /b 1
)
echo [ok] 已删除。
exit /b 0
