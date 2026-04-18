# Phase 4：定时同步（操作系统计划任务）

本文档说明如何用 **Windows 任务计划程序**、**cron** 或 **systemd timer** 定期调用现有 CLI，实现无人值守的增量同步。决策背景见 [specs/roadmap.md](../specs/roadmap.md) Phase 4。

## 1. 标准入口

与手动执行相同，推荐在计划任务里运行的命令为：

```bash
uv run onelap2strava sync --incremental
```

- **`--incremental`**：只处理「自上次成功同步以来」在顽鹿侧出现的新骑行（依赖 `data/.sync.db`），可安全重复执行。
- **「每隔多久 / 每天几点跑」**：由**操作系统调度器**配置，**不是**本 CLI 的子参数（避免与一次性 `sync` 进程语义重复，也不引入常驻进程）。

### 仓库根目录的便捷脚本（可选）

若不想在图形界面里逐字段配置，可使用仓库根目录下的脚本——它们**只负责注册系统调度**，实际执行的仍是 `uv run onelap2strava sync --incremental`：

| 文件 | 作用 |
| --- | --- |
| `run-incremental-sync.cmd` | Windows：单次同步；计划任务应指向此文件（脚本内 `cd` 到仓库根）。 |
| `install-scheduled-sync-windows.cmd` | Windows：编辑脚本顶部的 `SYNC_MODE`（`hourly` 每 N 小时 / `daily` 每天固定时刻）、`HOURLY_INTERVAL`、`DAILY_TIME`，运行一次即可 `schtasks /create`。`install-scheduled-sync-windows.cmd uninstall` 删除同名任务。 |
| `run-incremental-sync.sh` | Linux/macOS：单次同步（需本机已安装 `uv` 且在非 cron 环境下能解析 PATH）。 |
| `install-scheduled-sync-unix.sh` | Linux/macOS：编辑脚本内变量（或 `SYNC_MODE=daily DAILY_AT=07:30 ./install-scheduled-sync-unix.sh`）写入当前用户 crontab；默认日志 `data/sync-cron.log`。`--remove` 移除本脚本写入的条目。 |

仍可直接按下面各节**手动**配置任务计划、`cron` 或 systemd，与使用便捷脚本二选一即可。

## 2. 运行前检查

| 项目 | 说明 |
| --- | --- |
| **工作目录** | 任务应能解析到**项目根目录**（含 `pyproject.toml`、`.env`、`data/`）。相对路径的 token、Cookie、SQLite 都落在 `data/` 下，若「起始于」错误会导致找不到文件或写到别处。 |
| **`uv` 与 PATH** | 计划任务、cron、systemd 的环境往往比交互式 shell 更「干净」。若提示找不到 `uv`，在任务里写 `uv` 的**绝对路径**，或先用脚本 `cd` 到项目再调用。 |
| **顽鹿 Cookie** | 定时任务**不能**自动续期。会话过期时行为与手动运行一致（报错提示重跑 `onelap-login`）。 |
| **机器休眠** | 若任务安排在休眠时段，可能不执行；属调度器行为，与 CLI 无关。 |

## 3. 退出码约定

便于任务计划程序根据返回值重试或发通知：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。`--incremental` 且无新活动时也会打印 `No new activities since last sync.` 并以 `0` 退出。 |
| `1` | 本次同步中有失败条目，或 Strava/顽鹿认证与接口错误。 |
| `2` | CLI 用法错误（例如同时传入 `--incremental` 与 `--n`）。 |

## 4. Windows：任务计划程序

1. 打开「任务计划程序」→「创建任务…」（建议用「创建任务」以便完整配置触发器与条件）。
2. **常规**：可勾选「不管用户是否登录都要运行」若需后台跑（需保存密码）；仅当前用户会话时选「只在用户登录时运行」通常更简单。
3. **触发器**：按需设置「每天」「每周」或「按重复任务间隔」等——**间隔在此配置**，不要指望 CLI 再提供一份。
4. **操作** → 「启动程序」：
   - **程序或脚本**：`cmd.exe`
   - **添加参数**（将 `E:\Code\Onelap2Strava` 换成你的仓库绝对路径）：

     ```
     /c cd /d E:\Code\Onelap2Strava && uv run onelap2strava sync --incremental
     ```

   - **起始于（可选）**：`E:\Code\Onelap2Strava`  
     与上面 `cd` 二选一即可；显式设置「起始于」有助于部分场景下相对路径解析一致。

5. **条件 / 设置**：笔记本可取消「只有交流电源才启动」等，避免接电策略导致任务跳过。
6. **历史记录**：若需排查失败，在任务属性中启用「如果任务失败，按以下方式重新启动」或查看「任务计划程序」事件；也可把输出重定向到日志文件（用包装 `.cmd` 封装 `>> log.txt 2>&1`）。

若 `uv` 不在系统 PATH 中，把 `uv run` 换成 `uv` 可执行文件的完整路径（在资源管理器或 `where uv` 中确认）。

## 5. Unix：cron（Linux / macOS）

在 `crontab -e` 中增加一行（路径按本机修改）：

```cron
15 */4 * * * cd /home/you/Onelap2Strava && /home/you/.local/bin/uv run onelap2strava sync --incremental >> /home/you/Onelap2Strava/data/sync-cron.log 2>&1
```

- 上例表示：每 4 小时、在每小时的第 15 分执行（示例；请按需要改五段式）。
- **务必**在命令前 `cd` 到项目根，否则相对路径会错。
- `uv` 路径可用 `which uv` 查询。

### macOS（可选）

与上相同，可用 `crontab -e`；或用 **launchd**：在 `~/Library/LaunchAgents/` 放置 plist，`ProgramArguments` 里使用项目根的绝对路径调用 `uv run`（launchd 不保证默认工作目录即仓库根，建议在参数里显式 `cd` 或把 `WorkingDirectory` 键设为项目根）。调度可用 `StartCalendarInterval`（类日历触发）或 `StartInterval`（固定间隔秒数）。说明见 Apple 文档 *Creating Launch Daemons and Agents*。

## 6. Linux：systemd 用户定时器（可选）

比 cron 更易与日志、依赖统一管理。示例：每天固定时刻执行（路径自行替换）。

`~/.config/systemd/user/onelap-sync.service`：

```ini
[Unit]
Description=Onelap incremental sync to Strava

[Service]
Type=oneshot
WorkingDirectory=/home/you/Onelap2Strava
ExecStart=/home/you/.local/bin/uv run onelap2strava sync --incremental
```

`~/.config/systemd/user/onelap-sync.timer`：

```ini
[Unit]
Description=Daily Onelap sync

[Timer]
OnCalendar=*-*-* 22:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now onelap-sync.timer
```

## 7. 排障（无 `--dry-run`）

当前版本**没有** `sync --dry-run`。可按需：

- 查看本地同步历史：`uv run onelap2strava sync-log`
- 查看顽鹿最近活动（不落库）：`uv run onelap2strava onelap-list`
- 打开调试日志（`-v` 为**全局**选项，放在子命令前）：

  ```bash
  uv run onelap2strava -v sync --incremental
  ```

## 8. 与 Phase 5 的边界

需要**浏览器内粘贴 Cookie / 托管形态下的定时**时，属于路线图中的 Web 化阶段；本阶段仅保证 CLI + 系统调度即可复用同一套增量语义。
