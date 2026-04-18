# batchfiles

与**定时增量同步**相关的 Windows 批处理与 Unix shell：注册系统计划任务或 `crontab`，实际仍执行仓库根目录下的 `uv run onelap2strava sync --incremental`。

推荐从仓库根用 **`uv run onelap2strava auto-sync install`**（参数 `--mode` / `--every` / `--at`），内部会调用本目录脚本。亦可直接运行 `install-scheduled-sync-*.cmd` / `.sh`。

使用说明见主 [README.md](../README.md)「定时自动同步」与 [contexts/phase4-scheduled-sync.md](../contexts/phase4-scheduled-sync.md)。
