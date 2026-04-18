# Phase 4：会话归纳：定时同步（操作系统计划任务）

> 本文档沉淀 Phase 4 从路线图定义、对话讨论到代码/脚本交付的全过程，写法延续 [phase3.1-dedupe-and-resilience.md](phase3.1-dedupe-and-resilience.md)（偏重**为什么**、**取舍**、**实际落地 vs 原计划**）。后半部分为面向用户的**操作说明**（标准命令、退出码、各平台手工配置、排障）。路线图原文见 [specs/roadmap.md](../specs/roadmap.md) Phase 4。

## 1. 路线图里 Phase 4 要解决什么

**目标**：在**不依赖 Web 界面**的前提下，让同步按日程自动跑，减轻「想起来才手动 `sync`」的遗漏。

**推荐技术路线**（与 roadmap 一致）：优先 **Windows 任务计划程序**、**cron** 或 **systemd.timer** 调用**已有**的一次性 CLI——**零新增 Python 依赖、无常驻进程**；「每隔多久 / 每天几点」由**系统调度表达式**表达，而不是在应用内再实现一套调度器。

**前置条件**（Phase 3 已具备）：`sync --incremental` + 本地 SQLite 同步日志使单次触发**幂等、可安全重跑**；失败重试与去重已在链路内，适合「到点触发」而非「人肉触发」。

**明确不做**（与 Phase 5 分界）：顽鹿 Cookie **浏览器内续期**、为续期引入 Playwright/WebView 等——归入 Phase 5；定时任务**不能替代** Cookie 续期，过期时行为与手动运行相同。

## 2. 对话中的关键结论：要不要新子命令、间隔进不进 CLI

### 2.1 不需要单独的「定时同步」CLI 后缀

「定时」描述的是**何时再调起进程**，与「同步哪些活动」正交。增量语义已由 **`sync --incremental`**（+ `data/.sync.db`）表达；若再增加 `--scheduled` 或 `sync-cron` 子命令，会与 `--incremental` **语义重复**，还要多维护文档与测试。仓库外层的 `.cmd`/`.ps1` 只做 `cd` + `uv run` 属于**脚本包装**，不必进 Python 包。

### 2.2 「间隔、每天几点」不进 `sync` 子参数

在**推荐路径**（系统计划任务 + 一次性进程）下，`sync` 是**跑完即退出**的命令；**「隔多久再执行下一次」**只能由 **cron / 任务计划 / systemd** 决定。若在 `sync` 上加 `--interval` 而无长驻进程，要么**无效**（单次运行仍只执行一次），要么被迫做成**自循环常驻进程**——与 roadmap「优先系统任务、无常驻进程」相悖。

**可选的未来**（非 Phase 4 门槛）：若将来做进程内 APScheduler 或 `daemon` 子命令，再在**那条入口**上提供 `--interval` / `--cron` 才与一次性 `sync` 区分清楚。

**曾讨论、未纳入交付的防护**：若担心用户把 cron 配成「每分钟一次」而频繁打顽鹿接口，可另做「距上次同步不足 T 秒则 noop 退出」——属于防误配与限流，需单独产品决策；**不能替代**用户在调度器里把间隔设合理。

## 3. 从「纯文档」到 batchfiles 与 `auto-sync` 的演进

### 3.1 第一版交付：文档 + README 对齐

首版 Phase 4 交付物是 **文档**：`sync --incremental` 作为定时任务唯一标准入口；说明工作目录、`PATH`/`uv`、分平台可抄范例；README 路线图与 [specs/roadmap.md](../specs/roadmap.md) 的 Phase 4/5 定义对齐；FAQ 固化 **`sync` 退出码**（`0` 成功含无新活动 / `1` 失败或认证错误 / `2` 参数冲突）；路线图中的可选 **`sync --dry-run`** 未实现，排障沿用 `sync-log`、`onelap-list`、全局 `-v`。

### 3.2 batchfiles：脚本收拢与仓库根工作目录

随后增加 **`batchfiles/`** 目录，集中 Windows `.cmd` 与 Unix `.sh`：

- **`run-incremental-sync.*`**：单次执行增量同步；因脚本位于子目录，内部 **`cd` 到上一级（仓库根）**，再 `uv run onelap2strava sync --incremental`。
- **`install-scheduled-sync-windows.cmd`**：调用 `schtasks` 注册计划任务；支持脚本内默认或命令行 **`hourly N` / `daily HH:mm`**；`uninstall` 删除任务。
- **`install-scheduled-sync-unix.sh`**：写入当前用户 **crontab**；同样支持 **`hourly`/`daily` 参数**或环境变量；默认日志 **`data/sync-cron.log`**（已 [gitignore](../.gitignore)）；`--remove` 移除带标记行。

文档中补充 **macOS**：可与 Linux 一样用 `cron`，或用 **launchd**（plist + `WorkingDirectory` / 绝对路径）。

### 3.3 `auto-sync` 子命令：CLI 参数转调 batchfiles

为满足「用一条命令配置间隔与每日时刻」的诉求，在 [`cli.py`](../src/onelap2strava/cli.py) 增加 **`auto-sync`**：`install` / `uninstall` 通过 **`subprocess`** 调用上述脚本（**不重复实现** `schtasks`/crontab 逻辑）。`install` 侧提供 **`--mode hourly|daily`**、**`--every`**（小时）、**`--at`**（`HH:MM`）；仅在本仓库检出且存在 **`batchfiles/`** 时可用（可编辑安装下 `Path(__file__)` 相对仓库根解析）。

**`--help` 文案**：子命令与参数的说明使用**英文**（与用户在终端里对 `--help` 的期望一致）；运行时错误信息仍可与项目其他子命令一样使用中文。

### 3.4 脚本终端输出语言

`batchfiles/` 内 **`echo` 与用户可见提示**统一为**英文**，避免与 `auto-sync --help` 语言混用；[`batchfiles/README.md`](../batchfiles/README.md) 同步为英文简介。

## 4. 实际落地与路线图偏差（诚实记录）

| 维度 | 原计划侧重 | 实际落地 |
| --- | --- | --- |
| 交付物形态 | 「命令 + 文档」，未强制改 pyproject | 文档 + **`batchfiles/`** + **`auto-sync`** 薄封装（仍无新第三方依赖） |
| 调度参数位置 | 系统调度器 | 未变；额外提供 CLI/脚本参数**生成**系统任务或 crontab，而非在 `sync` 上增加 interval |
| `--dry-run` | roadmap 可选 | **未做**；排障路径见下文 §操作说明「排障」 |
| Phase 编号 | README 曾残留旧「Phase 4 Web」表述 | 已与 roadmap 对齐为 **Phase 4 = 定时同步、Phase 5 = Web 化** |

## 5. 验收与测试线索

- **`uv run pytest`**：全量测试通过（含 `tests/test_cli.py` 中对 `auto-sync` 委托与参数校验的用例）。
- **行为**：无新活动时 `sync --incremental` **退出 0**；有失败或认证问题 **退出非 0**，便于计划任务根据返回值告警或重试。

## 6. 对后续迭代的提示

- **仅装 wheel、无仓库树**：`auto-sync` 可能找不到 `batchfiles/`——若未来要支持，需把脚本随包分发或改为 Python 内直接调 `schtasks`/写 crontab（重复逻辑会增多）。
- **常驻进程调度**（APScheduler 等）：若做，与 Phase 4 的「OS 调度优先」并行存在，需在文档里写清适用场景，避免两套语义抢同一用户心智。

## 7. 对话里值得保留的判准

- **调度与语义分层**：「拉哪些活动」是 `sync --incremental`；「多久跑一次」是 OS 或安装脚本/CLI 的调度层——混进 `sync` 会混淆进程边界。
- **加包装不等于加产品面**：`batchfiles/` 与 `auto-sync` 是**薄委托**，核心同步逻辑仍在 Phase 1–3 链路，符合「不把调度复杂度塞进 `sync.py`」。
- **英文 surface 的一致性**：用户可见的 `--help` 与 batchfiles 终端输出统一英文，减少同一操作多语言混排。
- **续期永远不是定时任务的副作用**：与 Phase 2/3 结论一致，Cookie 过期仍走 `onelap-login`；自动续期留在 Phase 5 评估。

---

## 操作说明

### 1. 标准入口

与手动执行相同，推荐在计划任务里运行的命令为：

```bash
uv run onelap2strava sync --incremental
```

- **`--incremental`**：只处理「自上次成功同步以来」在顽鹿侧出现的新骑行（依赖 `data/.sync.db`），可安全重复执行。
- **「每隔多久 / 每天几点跑」**：由**操作系统调度器**配置，**不是**本 CLI 的子参数（避免与一次性 `sync` 进程语义重复，也不引入常驻进程）。

### `batchfiles/` 便捷脚本（可选）

若不想在图形界面里逐字段配置，可优先使用 **`uv run onelap2strava auto-sync install`**（参数 `--mode hourly|daily`、`--every`、`--at`），由 CLI 转调本目录脚本；亦可直接运行下表中的文件。

脚本**只负责注册系统调度**，实际执行的仍是 `uv run onelap2strava sync --incremental`（工作目录为**仓库根**，包装脚本已 `cd` 到仓库根）：

| 文件 | 作用 |
| --- | --- |
| `batchfiles/run-incremental-sync.cmd` | Windows：单次同步；计划任务应指向此文件。 |
| `batchfiles/install-scheduled-sync-windows.cmd` | Windows：编辑脚本顶部的 `SYNC_MODE`（`hourly` 每 N 小时 / `daily` 每天固定时刻）、`HOURLY_INTERVAL`、`DAILY_TIME`，运行一次即可 `schtasks /create`。`batchfiles\install-scheduled-sync-windows.cmd uninstall` 删除同名任务。 |
| `batchfiles/run-incremental-sync.sh` | Linux/macOS：单次同步（需本机已安装 `uv` 且在非 cron 环境下能解析 PATH）。 |
| `batchfiles/install-scheduled-sync-unix.sh` | Linux/macOS：编辑脚本内变量（或 `SYNC_MODE=daily DAILY_AT=07:30 ./batchfiles/install-scheduled-sync-unix.sh`）写入当前用户 crontab；默认日志 `data/sync-cron.log`。`--remove` 移除本脚本写入的条目。 |

仍可直接按下面各节**手动**配置任务计划、`cron` 或 systemd，与使用便捷脚本二选一即可。

安装类脚本亦支持命令行 **`hourly N`** / **`daily HH:MM`**（与 `auto-sync` 传入参数一致），无需改脚本内默认值。

### 2. 运行前检查

| 项目 | 说明 |
| --- | --- |
| **工作目录** | 任务应能解析到**项目根目录**（含 `pyproject.toml`、`.env`、`data/`）。相对路径的 token、Cookie、SQLite 都落在 `data/` 下，若「起始于」错误会导致找不到文件或写到别处。 |
| **`uv` 与 PATH** | 计划任务、cron、systemd 的环境往往比交互式 shell 更「干净」。若提示找不到 `uv`，在任务里写 `uv` 的**绝对路径**，或先用脚本 `cd` 到项目再调用。 |
| **顽鹿 Cookie** | 定时任务**不能**自动续期。会话过期时行为与手动运行一致（报错提示重跑 `onelap-login`）。 |
| **机器休眠** | 若任务安排在休眠时段，可能不执行；属调度器行为，与 CLI 无关。 |

### 3. 退出码约定

便于任务计划程序根据返回值重试或发通知：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。`--incremental` 且无新活动时也会打印 `No new activities since last sync.` 并以 `0` 退出。 |
| `1` | 本次同步中有失败条目，或 Strava/顽鹿认证与接口错误。 |
| `2` | CLI 用法错误（例如同时传入 `--incremental` 与 `--n`）。 |

### 4. Windows：任务计划程序

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

### 5. Unix：cron（Linux / macOS）

在 `crontab -e` 中增加一行（路径按本机修改）：

```cron
15 */4 * * * cd /home/you/Onelap2Strava && /home/you/.local/bin/uv run onelap2strava sync --incremental >> /home/you/Onelap2Strava/data/sync-cron.log 2>&1
```

- 上例表示：每 4 小时、在每小时的第 15 分执行（示例；请按需要改五段式）。
- **务必**在命令前 `cd` 到项目根，否则相对路径会错。
- `uv` 路径可用 `which uv` 查询。

#### macOS（可选）

与上相同，可用 `crontab -e`；或用 **launchd**：在 `~/Library/LaunchAgents/` 放置 plist，`ProgramArguments` 里使用项目根的绝对路径调用 `uv run`（launchd 不保证默认工作目录即仓库根，建议在参数里显式 `cd` 或把 `WorkingDirectory` 键设为项目根）。调度可用 `StartCalendarInterval`（类日历触发）或 `StartInterval`（固定间隔秒数）。说明见 Apple 文档 *Creating Launch Daemons and Agents*。

### 6. Linux：systemd 用户定时器（可选）

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

### 7. 排障（无 `--dry-run`）

当前版本**没有** `sync --dry-run`。可按需：

- 查看本地同步历史：`uv run onelap2strava sync-log`
- 查看顽鹿最近活动（不落库）：`uv run onelap2strava onelap-list`
- 打开调试日志（`-v` 为**全局**选项，放在子命令前）：

  ```bash
  uv run onelap2strava -v sync --incremental
  ```

### 8. 与 Phase 5 的边界

需要**浏览器内粘贴 Cookie / 托管形态下的定时**时，属于路线图中的 Web 化阶段；本阶段仅保证 CLI + 系统调度即可复用同一套增量语义。
