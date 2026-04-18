# 顽鹿运动 → Strava 演进路线图

本文档记录整个产品的五阶段演进规划，作为后续迭代的"北极星"。每个 Phase 都列出目标、范围边界、关键技术点、交付标准和本阶段明确不做的事，避免提前引入复杂度。

> **状态约定**：✅ 已完成 / 🚧 进行中 / 📋 计划 / 💡 可选。
> 每个已完成的 Phase 配一个"**实际落地与偏差**"小节，诚实记录跟原计划的差异——路线图的价值在"指北"而不是"打卡"，差异本身往往比原计划更值得参考。

---

## Phase 1 ✅：离线脚本打通链路

### 目标
在本地跑通"顽鹿 Fit 文件 → 坐标修正 → Strava 上传"的完整链路，验证所有核心技术难点可解。

### 范围边界
- Fit 文件由用户**手动**从顽鹿 App 导出并放到本地指定目录。
- 单用户、命令行、本机运行。
- Strava 授权走一次 OAuth，token 持久化到本地 JSON。

### 关键技术点
- GCJ-02 ↔ WGS-84 双向坐标转换（迭代反解法）。
- Fit 文件解析/修改/回写（semicircles 单位处理、保留 HR/功率/踏频等字段）。
- Strava OAuth 2.0（本地 HTTPServer 接收回调）。
- Strava 上传接口 + 去重检测（external_id + 时间窗）。
- 真实数据回归测试：以"同一路线两次骑行"夹具验证 GCJ-02 偏移被消除。

### 交付标准
- `uv sync` 后一键可运行。
- `uv run onelap2strava upload <fit>` 成功返回 Strava activity URL。
- Strava 上看到的路线与顽鹿 App 中路线位置一致（在正确的道路上）。
- 有完整的 README 和夹具回归测试。

### 实际落地与偏差
- ✅ 全部按计划交付。`tests/test_fit_fixer.py` 用真实夹具验证：平均偏移向量模长 221 m → 0.62 m，P50 距离 306 m → 2.4 m，**系统性偏移降低约 356 倍**，残留落到 GPS 噪声量级。
- 坐标函数的 `in_china` 粗边界判断让海外骑行原样 passthrough——这是预期外但正确的补强。
- Strava token 自动刷新做在 `strava_auth.py`，用户对 6 小时过期无感。
- 决策过程和踩坑沉淀在 [contexts/phase1-offline-script.md](../contexts/phase1-offline-script.md)。

---

## Phase 2 ✅：自动化顽鹿数据拉取

### 目标
移除"手动导出 Fit"这一步，实现登录顽鹿后自动获取最新骑行的 Fit 文件。

### 范围边界
- 仅对接顽鹿**私有接口**（无公开 API）。
- 仍是本地 CLI，用户提供顽鹿账号凭证（本地保存，不上云）。
- 仅拉取最新 N 条骑行记录（默认 1 条）。

### 原计划 vs. 实际落地（诚实记录）

| 维度 | 原计划 | 实际落地 | 原因 |
| --- | --- | --- | --- |
| 接口侦察 | Charles / mitmproxy 抓 App 或小程序 | **DevTools + 公开项目 [`moruoxian/SyncOnelapToXoss`](https://github.com/moruoxian/SyncOnelapToXoss)** | 发现顽鹿有 Web 端 `u.onelap.cn/analysis`，直接比抓包省 2–4 小时 |
| 登录实现 | 密码登录 + `keyring` 加密凭证 | **手粘浏览器 Cookie** | CSDN 博客写的 `/api/login` 从未一手验证过；实测阶段还反向印证博客连 Cookie 键名都写错了（说是 `PHPSESSID`，实际是 `OTOKEN`）。手粘 Cookie 对顽鹿登录实现完全免疫 |
| 凭证自动刷新 | token 过期自动刷新 | **Cookie 过期时报错要求重粘** | Cookie 寿命实测数天～一周以上，手动重粘 10 秒搞定，不值得为"自动刷新"引入对私有登录接口的依赖 |
| 会话过期识别 | 按 401/403 判定 | **增加 `200 + Content-Type: text/html` 分支** | 实测顽鹿会话过期返回的是 "跳登录页" 而非 401，双重识别写进 `client.py::_raise_if_auth_required` |

**中途迭代过一次又撤回**：v2 阶段引入过 `--from-browser`（基于 `browser-cookie3` 自动读浏览器 Cookie DB），实测 Chrome/Edge 125+ 的 App-Bound Encryption 在 Windows 上即便以管理员身份也常解不出密钥，维护"时灵时不灵"的推荐路径对体验是净负收益。**v3 彻底下线**，回到"手粘唯一"。完整过程见 [`contexts/phase2-onelap-scraping.md`](../contexts/phase2-onelap-scraping.md) §2.6 + §2.7。

### 关键技术点（终态）
- Cookie 字符串解析 + 全量透传（不挑键名，规避顽鹿字段变动）。
- 接入层严格隔离在 `src/onelap2strava/onelap/`：client / auth / models / 业务层（sync）分离。
- 会话过期双重识别（HTML 响应 + 401）。
- 下载原子写（`.fit.part` → rename）避免半截文件被误认作缓存命中。
- live probe：`onelap-login` 保存 Cookie 后立刻打一次 `/analysis/list`，失败的 Cookie 在入库时就被拦住。
- `data/cache/*.fit` 作为"已处理记录"的天然记账点，为 Phase 3 的 SQLite 日志铺路。

### 交付标准（全部达成）
- ✅ `uv run onelap2strava sync` 一条命令完成：拉最新 Fit → 坐标修正 → 上传 Strava。
- ✅ Cookie 过期时明确提示 `cookies likely expired`，用户重跑 `onelap-login` 即可。
- ✅ 20 个 Phase 2 测试全绿（onelap 接口层 17 + 同步流水线 3），总测试数 41。
- ✅ 依赖保持轻量：`uv sync` 不拖任何二进制 wheel（撤回 `[browser]` extra 后）。

### 本阶段不做（保留到后续）
- 历史全量同步（只同步最新 N 条）。
- 密码登录 / API 登录（除非未来一手抓包确认）。
- 公开分发（避免账号安全/合规风险）。

### 风险与备注
- 顽鹿私有接口随时可能改版——接入层边界已清晰，改版时只改 `src/onelap2strava/onelap/` 一个目录。
- ABE 让"跨进程读浏览器 Cookie DB"在 Windows 上不稳定，**任何阶段的**自动续期方案**不应**再走这条路径（若 Phase 5 做浏览器体验，也以页面内交互为主，而非偷读 Cookie DB）。

---

## Phase 3 ✅：去重与容错增强

### 目标
应对长期使用中的真实场景：避免重复上传、应对各种失败情况、减少 Cookie 过期时的 UX 摩擦。

### 范围边界
- 仍是单用户 CLI 工具。
- 重点增强**同步链路**的健壮性和**登录态**的耐用性（会话过期时的 CLI 体验仍是明确报错 + `onelap-login` 手粘；**需浏览器 / 页面的流畅续期**归 **Phase 5**）。

### 关键技术点（原规划，供参考）
- **模糊去重**：目前 Phase 1 的"时间 ±10 分钟 + external_id sha1"够用但不够强——顽鹿可能对同一骑行重新导出（字节不同但内容几乎一致）。升级为"开始时间 ±10 分钟 **+** 总时长差 <5% **+** 起点距离 <500m"三元组。
- **失败重试**：上传超时/网络失败自动重试（指数退避，最多 3 次）。`sync.py::_sync_one` 外层加即可，当前单条失败已不阻塞其它，但没重试。
- **本地 SQLite 同步日志**：记录每次同步的 Fit 哈希 / Strava activity ID / 时间戳，便于审计、回溯、支撑模糊去重。`data/cache/` 目录天然是"已处理记录"的源头。
- **增量同步**：Phase 2 的"最新一条"扩展为"自上次同步以来的所有新骑行"，配合 SQLite 日志做断点。
- **Cookie**：CLI 不重做 `browser-cookie3`（Phase 2 v3 已证伪 ABE）；不设 CLI 内嵌 WebView——与 **Phase 5** 一并考虑（工程原则 §6）。

### 实际落地与偏差

去重、容错、日志与增量共用一张 SQLite 表，在同一轮迭代交付。**需浏览器维度的顽鹿续期与录入**划入 **Phase 5**，与 Strava OAuth、网页表单同一套交互，避免 CLI 单独背 Playwright/pywebview；决策细节见 [contexts/phase3.1-dedupe-and-resilience.md §1](../contexts/phase3.1-dedupe-and-resilience.md)。

### 交付清单
- **本地 SQLite 同步日志**：`data/.sync.db` 单表 `synced_activities`，主键 `onelap_activity_id`，status 列区分 ok / duplicate / failed / backfilled。首次启用自动扫描 `data/cache/` 做 backfill，模糊去重从第一次就生效。
- **三层去重**（不是替换而是叠加）：本地模糊三元组（新）→ Strava `get_activities ±10min`（Phase 1 原有）→ Strava `external_id` sha1（Phase 1 原有）。本地命中即跳过后两层 Strava 查询，`--force` 同时绕过前两层保留最后一层。
- **失败重试**：`_with_retry` 指数退避 1s/2s/4s，白名单重试 `ConnectionError` / `Timeout` / `ChunkedEncodingError`；auth 错误 / 4xx / `ValueError` 立即失败，避免重试放大副作用。
- **增量同步**：`sync --incremental` 按 `onelap_activity_id` 过滤已处理活动。保持 `--n 1` 为默认不变，两者互斥（`--incremental --n 5` 直接 exit 2）。
- **新增子命令 `sync-log`**：列出最近 N 行，用于"上次同步跑到哪"的审计。

### 与原计划的偏差（诚实记录）

| 维度 | 原计划 | 实际落地 | 原因 |
| --- | --- | --- | --- |
| 去重策略 | "**升级为**三元组"，听起来替换 | **在原有两层 Strava 去重上 叠加** 一层本地模糊 | 启发式不是正确性边界；多一层服务端兜底才敢让本地大胆跳过 Strava 查询（[phase3.1 §2.2](../contexts/phase3.1-dedupe-and-resilience.md)） |
| SQLite 字段 | "Fit 哈希 + activity id + 时间戳" 三字段 | 九字段（加 duration / start_lat / start_lng / status / synced_at） | 模糊去重和增量要一张表服务两种读；status 列撑起差异化查询，避免建第二张表 |
| 模糊去重位置 | 未明确 | **在 `fix_fit` 之前**，用 raw（GCJ-02 帧）fit 的坐标查 | 命中时跳过 fix + 磁盘写；backfill 读的也是 raw 文件，帧一致；500m 阈值对 300m 的 GCJ 偏差有余量 |
| `--incremental` 默认 | 未明确 | **保守：保留 `--n 1` 默认**，`--incremental` 是 opt-in | Phase 2 用户已习惯"最新 1 条"默认；突然变"拉全部新"可能惊到老用户 |
| Cookie 续期（浏览器/WebView） | 本阶段要做 | **延后到 Phase 5** | 与 Web 化一并实现续期与录入，避免 CLI 单独背 Playwright/pywebview |

### 交付证据
- ✅ `uv run pytest` 72 个测试全绿（Phase 1/2 的 41 + Phase 3 的 31）。
- ✅ 零新增第三方依赖（`sqlite3` 在 stdlib；Haversine 复用 Phase 1 已有实现）。`pyproject.toml` 不改，`uv sync` 速度不退化。
- ✅ 连跑两次 `sync --incremental`，第二次输出 "No new activities since last sync." 且 `strava.upload_activity` / `get_activities` 零调用（测试 `test_incremental_skips_already_seen_activities` 断言）。
- ✅ 模拟 `ConnectionError` 两次后恢复：第三次成功并 `status=ok`；全程失败时日志 `status=failed` 等待下次重试。
- ✅ 决策沉淀：[contexts/phase3.1-dedupe-and-resilience.md](../contexts/phase3.1-dedupe-and-resilience.md)。

### 本阶段不做（保留到后续）
- 跨设备状态同步。
- Web UI（含需浏览器的 Cookie 续期——见 Phase 5）。
- 顽鹿历史全量同步（只做"自上次以来的新骑行"，做"从零到今"要另开一轮，顽鹿是否支持分页仍待确认）。

---

## Phase 4 ✅：定时同步（CLI / 常驻进程）

### 目标
在**不依赖 Web 界面**的前提下，让同步按日程自动运行：用户骑行后无需记得手动执行 `sync`，降低「想起来才同步」的遗漏。

### 可行性说明（结论：**可行**，且与现有架构匹配）
- **前置条件已具备**：Phase 3 的增量同步（`--incremental`）+ SQLite 日志使单次定时任务**幂等、可安全重跑**；失败重试与去重已在链路里，适合「到点触发」而非「人肉触发」。
- **推荐实现重心**：优先 **操作系统任务计划**（Windows「任务计划程序」、Linux/macOS `cron` 或 systemd timer）调用现有 CLI——**零新增 Python 依赖、无常驻进程**， Cron 表达式由系统表达，文档写清即可。
- **可选增强**：若未来需要「内置调度」或跨平台统一体验，再考虑进程内调度（如 APScheduler）或极轻 wrapper；那是优化项，不是 Phase 4 门槛。
- **与 Cookie 的关系**：定时任务**不能替代** Cookie 续期；会话过期时同步仍会失败并沿用 Phase 2/3 的明确报错。**需要调起浏览器或页面完成续期**的体验放在 **Phase 5**，避免本阶段为续期再引入 WebView/Playwright。

### 范围边界
- 仍以**单用户、本地 CLI** 为主；交付物可以是「命令 + 文档」而非必须改 pyproject。
- 调度策略（每天固定时刻 / 每 N 小时 / 开机后延迟一次）以**用户本机习惯**为准，不做云端统一队列。

### 关键技术点
- 文档化：`sync --incremental` 作为定时任务的标准入口；说明工作目录、环境变量、`PATH`/`uv run` 在计划任务中的写法（Windows 与 Unix 分列）。
- 日志与退出码：确保失败时进程非零退出，便于任务计划程序重试或发通知（若已有则引用，否则在本 Phase 补齐最小约定）。
- 可选：`--dry-run` 或与日志级别相关的开关，便于排查「为什么这次没跑」。
- **明确不引入**：为续期而起的浏览器自动化（归属 Phase 5）。

### 交付标准
- 用户可按文档在 Windows / Linux 上配置计划任务，实现无人值守的增量同步。
- 行为可预期：无新活动时安静退出；网络/Cookie 问题时失败可见（日志或退出码）。

### 实际落地与偏差
- ✅ 交付物为 **文档 + README 指引**：系统计划任务 / `cron` / `systemd.timer` 调用既有 `uv run onelap2strava sync --incremental`，不新增常驻进程、不新增定时类 CLI 参数（间隔与时刻由调度器表达）。详见 [contexts/phase4-scheduled-sync.md](../contexts/phase4-scheduled-sync.md)。
- **未实现**路线图中的可选 `sync --dry-run`；排障沿用 `sync-log`、`onelap-list` 与全局 `-v`（见上述文档 §7）。
- **与「CLI / 常驻进程」标题的关系**：推荐路径仍是 **一次性进程 + OS 调度**；若未来做进程内 APScheduler 等，属可选增强，非本阶段交付。

### 本阶段不做
- Web UI、多租户。
- 顽鹿 Cookie 的浏览器内续期（Phase 5）。
- 云端托管的「服务端 cron」（若做，属于 Phase 5 部署形态的一部分）。

---

## Phase 5 💡：Web 化（可选，视需要）

### 目标
把工具升级为可托管的 Web 服务，支持更友好的授权体验、（可能的）多用户场景，以及**与浏览器同屏的顽鹿 Cookie 录入 / 需要页面交互时的续期**（承接原计划在 CLI 用 WebView 解决的那部分体验，避免命令行单独背一套 Playwright/pywebview）。

### 范围边界
- 核心依旧是 Phase 1–4 的同步逻辑，Web 层是封装。
- 决定是"单用户自部署"还是"公开 SaaS"前，先按单用户自部署做，避免合规和账号安全风险。

### 关键技术点
- 后端：**FastAPI**（异步、OAuth 回调简单、自动文档）。
- 任务队列：Celery + Redis（或 FastAPI `BackgroundTasks` 起步）；**定时同步**也可由服务端调度复用 Phase 4 已验证的增量语义。
- 前端：React/Vue + Tailwind，或 HTMX + Jinja2（MVP 用 HTMX 更省事）。
- 存储：SQLite（单用户）或 PostgreSQL（多用户），复用 Phase 3 的同步日志 schema。
- 部署：Docker Compose，可放 Fly.io / Railway / 自有 VPS。
- 安全：Strava token 加密存储，顽鹿 Cookie 处理需极其谨慎（明确用户协议）。
- **凭证 UX**：浏览器内完成 Strava OAuth；顽鹿侧以**页面内粘贴或引导登录**替代 CLI 弹 WebView；与 Phase 2 起沿用的 live probe（`/analysis/list`）兼容。
- **顽鹿 Cookie（由原「CLI 弹窗续期」合并至此）**
  - **约束**：仍**不走** `browser-cookie3`（Phase 2 v3 已证伪 ABE）。
  - **CLI 仍可先行**：若未来抓包**一手确认**顽鹿存在 refresh 类接口，可在不改 Web 的前提下以轻量方式补 CLI——与网页续期互补。
  - **长期 CLI 用户**：过期仍可用 `onelap-login` 手粘；不强迫为了续期而上 Web。
  - **Web 侧重**：网页内粘贴 Cookie / 嵌入式登录页、「登录一次顽鹿 Web」引导；比 CLI 迷你窗更符合心智。

### 交付标准
- 浏览器里完成 Strava 授权和顽鹿 Cookie 录入（及过期后的再次录入）。
- 可查看同步历史、手动触发同步、开关自动同步。
- 单容器 `docker compose up` 启动。

### 本阶段不做
- 大规模多租户运营（如果走这步需要重新评估合规）。
- 付费功能。
- 移动端 App。

---

## 阶段之间的依赖与取舍

```
Phase 1 ✅ 打通所有技术难点
   │
   ▼
Phase 2 ✅ 自动化顽鹿侧（手粘 Cookie + sync 一条命令）
   │
   ▼
Phase 3 ✅ 去重 + 容错 + 增量（需浏览器的顽鹿续期 → Phase 5）
   │
   ▼
Phase 4 ✅ 定时同步（计划任务驱动 CLI，解放「记得跑 sync」）
   │
   ▼
Phase 5 💡 Web 化（按需；含浏览器侧 Cookie 续期与托管形态下的定时同步）
```

**每完成一个 Phase，先停下来用一段时间再决定是否做下一阶段**。Phase 1 到 Phase 2 的间隔验证了"手动导出 Fit" 确实是日常摩擦最大的一环；Phase 2 到 Phase 3 的间隔也应该积累足够的"重复上传真的发生了吗 / Cookie 真的几天过期一次吗"这类一手数据，再决定去重与后续体验的优先级。**Phase 4 与 Phase 5 可解耦**：长期只用 CLI 的用户只需做到 Phase 4；需要「打开网页就能续 Cookie」再考虑 Phase 5。

## 从两次迭代沉淀出的一些工程原则

来自 Phase 1、2 的实测——**它们比 roadmap 本身更指导后续迭代**，所以单独列在这里（完整推理见各 Phase 的 `contexts/` 文档）：

1. **不信的信息不写进代码**。CSDN 博客提到的 `/api/login` 因此一直没碰；实测阶段反向印证了这个克制的价值（博客连关键 Cookie 键名都写错了）。
2. **加一条路径，不要换一条路径——但也要敢撤掉证伪的路径**。Phase 2 v2 加过 `--from-browser`，v3 实测证伪后完全撤回；保留手粘作为唯一路径并不丢人，沉没成本不是拒绝撤回的理由。
3. **好的架构给你撤销决定的自由**。Phase 2 的 live probe 是方案 A 和手粘共享的权威裁判——正因为有它兜底，我们才敢直接删掉方案 A 而不怕产品变脆弱。
4. **错误消息好看 ≠ 问题解决**。为糟糕路径持续优化错误分类会让你误以为离解决更近；要敢承认"这条路径本质上不可靠"，而不是一直在打磨它的失败体验。
5. **对私有接口的假设最小化**。接入层不假设 Cookie 键名、不假设鉴权方式、不假设响应字段全集——Phase 3 即便加了 SQLite 日志和模糊去重，这条边界也不能破。
6. **依赖轻量化也是 UX**。Phase 2 v3 撤掉 `[browser]` extra 后 `uv sync` 从 ~30 秒降到 ~5 秒，这是实打实的用户体验改善。Phase 3 的 SQLite 已在 stdlib；若 Phase 5 引入 WebView/Playwright，同样要把「装起来很快」作为评估维度之一。
