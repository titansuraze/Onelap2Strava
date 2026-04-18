# Phase 3.1 会话归纳：去重与容错增强

> 本文档沉淀了 Phase 3.1 从规划到交付的关键决策，延续 [phase1-offline-script.md](phase1-offline-script.md) 和 [phase2-onelap-scraping.md](phase2-onelap-scraping.md) 的风格。Phase 3 原本是一个整体（roadmap §87-113），实际拆成 **3.1（本文：去重与容错）** 和 **3.2（未开：Cookie 续期）** 两次交付，理由见 §1。

## 1. 为什么拆成 3.1 和 3.2

Phase 3 roadmap 包含五件事：**SQLite 日志 / 模糊去重 / 失败重试 / 增量同步 / Cookie 续期**。前四件有清晰的共同基础（一张 SQLite 表服务前三件的读，第四件 `--incremental` 复用同一张表），Cookie 续期则是独立的 UX 问题，并且需要先有一段真实使用数据（到底 Cookie 几天过期一次？用户愿意为续期付多大依赖代价？）才能判断 WebView 方案的性价比。

把 Cookie 续期从本次交付里切出去有两个好处：

- **前四项可以在一个 PR 里自洽完成**，依赖只需 stdlib `sqlite3`（`pyproject.toml` 零改动）。
- **Cookie 续期的路径可以等数据**。Phase 2 §2.7 的教训是"对一条不确定性大的路径过早投入会付两次成本"——先让 3.1 上线跑一段，积累"真实的 Cookie 过期频率"，再决定 3.2 的形态。

## 2. 关键决策与取舍

### 2.1 一张表服务三种读

原始 roadmap §99 只说"SQLite 同步日志"，没定 schema。实际设计时发现模糊去重（按 start_time/duration/start_point 查）和增量同步（按 onelap_activity_id 查 seen 集合）可以共享同一张表——只是不同的查询入口。建两张表会立刻引发"失败行算哪张？backfill 行算哪张？"这种分类负担，而分成两张表的好处（查询快）在单机几十到几百条数据的体量下完全不成立。

最终 schema（见 [sync_log.py](../src/onelap2strava/sync_log.py)）：

```sql
CREATE TABLE synced_activities (
    onelap_activity_id  TEXT PRIMARY KEY,
    fit_sha1            TEXT NOT NULL,
    start_time_utc      TEXT NOT NULL,
    duration_s          INTEGER,        -- 允许 NULL：短骑行可能没有 SessionMessage
    start_lat           REAL,           -- 同上
    start_lng           REAL,
    strava_activity_id  INTEGER,        -- NULL 表示 backfilled 或 failed
    synced_at           TEXT NOT NULL,
    status              TEXT NOT NULL   -- ok | duplicate | failed | backfilled
);
```

`status` 列撑起了所有差异化查询：模糊去重只看 `ok|duplicate|backfilled`（不看 `failed` 以免 fake-dedup 真正需要重试的 ride），增量 seen 集只看 `ok|duplicate` 且排除 `backfilled:` 前缀（见 §2.3）。

### 2.2 三层去重的顺序

原 roadmap §97 说"把 Phase 1 的 ±10min + sha1 升级为三元组"——读起来像"用三元组替换原有两层"。实际设计选择了**加一层不换一层**：

```
本地模糊三元组（Phase 3.1 新）
  ↓ miss
Strava ±10min 查 get_activities（Phase 1 已有）
  ↓ miss
Strava external_id sha1 服务端兜底（Phase 1 已有）
```

原因和 Phase 2 §2.6 的设计原则一脉相承：**本地模糊三元组是启发式，Strava 侧 sha1 是正确性边界**。如果把 Strava 两层去重删掉只留本地模糊，那么一旦本地 DB 被误删、用户换机器、或启发式参数调错，就会出现真正的重复上传——这是不可逆的（Strava 不会自动合并重复 activity）。多一层服务端兜底，让启发式敢更大胆（本地命中就跳过 Strava 查询），反过来反而更鲁棒。

`--force` 的语义也保持一致：**同时绕过前两层**（本地模糊 + Strava ±10min），保留 `external_id` 的服务端兜底。用户真想强制重传时，不会因为残留的一层本地去重感到困惑。

### 2.3 backfill 的 id 命名

第一次启用 SQLite 时需要从 `data/cache/` 里已有的 Fit 回填历史。这些 Fit 没有可关联的 `onelap_activity_id`（原始下载只保留了文件名）。三种方案：

1. **生成合成 id**（如 `backfilled:<filename>`）——显式标记来源，模糊去重匹配到可以识别
2. **反查顽鹿列表匹配文件名**——靠谱但依赖网络，且文件名格式改过就挂
3. **留空或用哈希**——模糊去重照样命中，但审计时看不出"这是 backfill 还是真同步"

选了 1。关键细节：**`seen_onelap_ids()` 要排除以 `backfilled:` 开头的 id**——否则 backfill 行会污染增量过滤集（它们不可能匹配到真实 Onelap id，但如果将来顽鹿巧合返回一个叫 `backfilled:xxx` 的 id，不排除会导致 silent skip）。

### 2.4 重试的边界：白名单而不是黑名单

原 roadmap §98 只说"指数退避最多 3 次"。落地时最关键的决策是**哪些异常值得重试**：

- **黑名单做法**："除了 OnelapAuthRequired 之外都重试" —— 危险，一个未知的业务错误也会被重试三次，浪费时间 + 可能放大副作用
- **白名单做法**（最终选）：只重试明确的瞬时错误

具体白名单（见 [sync.py::RETRYABLE_EXCEPTIONS](../src/onelap2strava/sync.py)）：

```python
RETRYABLE_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)
```

`ValueError` / `FileNotFoundError` / `OnelapAuthRequired` / 4xx 不重试——理由对应 Phase 2 §2.7 的原则"错误消息好看不等于问题解决"：重试不会让 auth 过期自己恢复，也不会让一个缺失文件突然出现。

### 2.5 模糊去重放在 fix 之前还是之后

`_sync_one` 的原流程是 `download → fix → upload`。模糊去重插入的位置有两个候选：

| 位置 | 优点 | 缺点 |
| --- | --- | --- |
| fix 之后（用 WGS-84 坐标查） | 和 Strava 侧比对的坐标帧一致 | 即便命中也白费一次 fix（写 `data/output/<stem>.fixed.fit`） |
| fix 之前（用 GCJ-02 坐标查） | 命中时跳过 fix，纯省事 | 如果 log 里也存的是 GCJ-02，帧一致；若混帧会有 200-300m 偏差 |

选了"fix 之前"，**并统一用下载的 raw 文件坐标（GCJ-02 帧）入库**。理由：

- 500m 阈值相对 300m 的最大 GCJ 偏差有余量，就算理论上混了帧也不会假阴性。
- backfill 读的也是 `data/cache/` 里的 raw 文件，天然 GCJ-02。同帧。
- 命中时节省一次 fit 解析 + 一次磁盘写——对老用户（fuzzy 命中占比高）是明显提速。

### 2.6 `--incremental` 的默认行为保持不变

Plan 阶段问过用户：`sync` 默认是否改成增量？最终选择是**保持 `--n 1` 默认，新增 `--incremental` flag**。保守选择的理由：

- Phase 2 的"最新 1 条"是经过验证的默认；用户已经形成习惯。
- 增量模式在第一次使用（空 log）时行为和"拉全部"等同，**如果默认打开**，新用户第一次跑可能会被大量活动惊到。
- `--incremental --n 5` 这种组合没有清晰语义（"自上次以来但最多 5 条"？），CLI 直接报错拒绝（exit 2），让用户显式选边。

### 2.7 测试隔离：每个 sync 测试用独立 DB

原有的 `tests/test_sync.py` 没有 SQLite 概念，run_sync 加上 SyncLog 后最先暴露的问题是**测试间状态污染**：如果不注入 `sync_log=` 或 `db_path=`，测试会默认用 `data/.sync.db`，跑两次顺序依赖。

解决办法是加一个 `sync_log` fixture（每个测试一个 `tmp_path / ".sync.db"`），整个文件所有 sync 测试都通过 fixture 注入。同时 `run_sync` 签名里 `sync_log` 参数是可选的（生产代码不注入，测试代码必须注入）——这个设计让测试的隔离意图**不可被忘记**：忘了注入的测试跑起来会污染真实 DB，开发环境会立刻察觉。

## 3. 踩过的坑（或避开的坑）

### 3.1 模糊去重参数在"共享夹具"测试下的 emergent behavior

测试 `test_incremental_processes_only_new_activities` 一开始假设"两条活动都会走完 upload 流程"，实际上 `_FakeOnelap` 里两条活动共享同一个 fit 夹具 —— start_time / duration / start_point 完全一样，第二条必然被模糊去重命中。

这个坑本身不是代码 bug，而是**测试意图与实现的不匹配**。原版测试试图同时验证两件事：(a) 增量过滤能过滤掉 seen id；(b) 未过滤的 id 都会走完上传。在共享夹具的约束下 (b) 不成立。

修正不是让 fuzzy dedup "只在非测试环境工作"（那是掩盖问题），而是**把测试意图削窄到只验证 (a)**：通过 `report.results` 的数量证明增量过滤确实发生在 per-activity 流水线之前。这条断言独立于模糊去重的行为，更稳固。

**教训**：当测试夹具的某个属性（共享 fit bytes）和被测功能的内部启发式（模糊去重）天然交互时，与其构造 "绕过交互" 的测试，不如承认这个交互、把断言削窄到不依赖它的那部分。

### 3.2 `duration_s` 和 `start_lat/lng` 允许 NULL 的决定

SessionMessage 里的 `total_elapsed_time` / `start_position_lat` 在非常短（几秒）的骑行里可能缺失。最初版本把这些列声明为 `NOT NULL`，跑 backfill 就触发了写入失败。

修正有两条思路：

1. 把列改成允许 NULL，模糊去重在 Python 侧处理"缺失时不 gate"的语义
2. 强制要求 fit 有完整 Session 数据才入库，否则跳过

选了 1。理由是"能记录 start_time 但没有 duration"的行仍然对审计有价值（`sync-log` 能看到"这次跑过"），而强行跳过会让日志失真。**启发式对"部分可比较"的数据要有降级策略**——和 Phase 2 §3.5 "启发式不是正确性边界"是同一个判断的两个侧面。

### 3.3 `INSERT OR REPLACE` 让 `failed → ok` 平滑

重试场景：活动 A 第一次上传失败，log 里存 `status=failed`；几小时后用户再跑 `sync`，这次网络好了，上传成功。这时需要把 failed 行覆盖成 ok。

三种做法：

1. 查旧行 + UPDATE / INSERT 分支
2. 先 DELETE 再 INSERT
3. `INSERT OR REPLACE`（选）

方案 3 一条 SQL 搞定。唯一要注意的：主键必须真实保证语义唯一（`onelap_activity_id`），否则"替换"会意外吞数据。实测 Onelap 返回的 activity id 是稳定唯一的，这个前提成立。

### 3.4 `with SyncLog.open(":memory:")` 的用途

最初 `SyncLog.open` 只接受 `Path`。测试里想要 "不落盘的极简测" 时很别扭（`tmp_path` 也挺便宜，但多了一次 IO）。后来让 `open` 接受字符串 `":memory:"`，走 sqlite3 的内存模式。

看似是个小细节，但它让 `test_in_memory_db` 这种"我只是想验证 API 能跑"的轻量测试不依赖文件系统。**保留一条轻量路径**是 Phase 2 §2.6 "加一条路径不要换一条路径" 在测试工程里的翻版。

## 4. 最终产物清单

### 代码

- [src/onelap2strava/sync_log.py](../src/onelap2strava/sync_log.py) — SyncLog 类、schema、fuzzy / seen-ids / backfill / recent
- [src/onelap2strava/sync.py](../src/onelap2strava/sync.py) — 重构：接入 SyncLog、`_with_retry`、模糊去重、增量、失败落日志
- [src/onelap2strava/fit_fixer.py](../src/onelap2strava/fit_fixer.py) — 新增 `read_fit_metadata` / `FitMetadata` / `_sha1_of_file`；扩展 `_extract_metadata` 返回 duration + start coords
- [src/onelap2strava/cli.py](../src/onelap2strava/cli.py) — `sync --incremental`（与 `--n` 互斥）+ 新增 `sync-log` 子命令

### 测试

- [tests/test_sync_log.py](../tests/test_sync_log.py) — 17 个：schema 幂等、三元组边界、backfill、seen/fuzzy 集隔离、持久化、内存模式
- [tests/test_sync.py](../tests/test_sync.py) — 扩展到 11 个：既有 3 条 + 模糊命中、`--force` 绕过、两次重试成功、重试耗尽、非瞬时错误不重试、增量过滤、backfill 自动触发
- [tests/test_cli.py](../tests/test_cli.py) — 新增 5 个：`--incremental` vs `--n` 互斥、`sync-log` 空/满、参数穿透
- [tests/test_fit_fixer.py](../tests/test_fit_fixer.py) — 加 `test_read_fit_metadata_on_real_fixture`

### 依赖

- **零新增 Python 依赖**。`sqlite3` 在 stdlib。延续 Phase 2 v3 "依赖轻量化也是 UX" 的原则。

### 文档

- [README.md](../README.md) — `sync --incremental` / `sync-log` / 三层去重 / 重试 / backfill 四个 FAQ 和项目结构更新
- [specs/roadmap.md](../specs/roadmap.md) — Phase 3 补"实际落地与偏差"，标注 3.1 完成 / 3.2 待评估
- [contexts/phase3.1-dedupe-and-resilience.md](phase3.1-dedupe-and-resilience.md) — 本文

### 运行时生成

- `data/.sync.db` — Phase 3.1 同步日志（gitignored）

## 5. 验收证据

```
> uv run pytest
72 passed in ~60s   (Phase 1/2 的 41 + Phase 3.1 的 31)

> uv run onelap2strava --help
# 9 个子命令：auth / fix / upload / upload-dir / onelap-login / onelap-list / sync / sync-log / token-info

> uv run onelap2strava sync --incremental
# 首次：扫 data/cache/ backfill + 同步未见过的所有新骑行
# 之后：仅同步"自上次以来"的新骑行，no-op 时提示 "No new activities since last sync."
```

## 6. 对 Phase 3.2 的建议

- **先收集数据**：连续用 3.1 版跑 2-4 周，记录 Cookie 实际过期频率。如果一周以上才过一次，WebView 方案的 ROI 就值得怀疑；如果几天一次，推动力才足。
- **不重走 `browser-cookie3` 的老路**：v2 的 ABE 撤回结论在 Phase 2 §2.7 里白纸黑字，3.2 的任何方案都应绕开"跨进程解密浏览器 Cookie DB"这个根本边界。
- **WebView 方案评估要算摩擦账**：`pywebview` / `playwright` 都要拖非轻量二进制（Chromium / WebView2 runtime），这跟 Phase 2 v3 "依赖轻量化"的收益方向相反。要量化对比"装 WebView 一次 vs 每 N 天手粘一次"哪个更磨人。
- **也别忽视更简的方向**：有没有可能顽鹿的 Cookie 其实有 refresh 语义？如果抓包确认有个等价 refresh 接口，比 WebView 轻得多。前提还是"一手抓包 + 一手验证"，不碰二手博客。

## 7. 对话里值得保留的思维方式

- **不换一条路径，加一层**：三层去重叠加而不是替换 Phase 1 的两层，延续 Phase 2 §2.6 的原则。多一层"启发式命中可以直接跳过权威层"反而比"只留启发式"更鲁棒——因为权威层是启发式出错时的撤销安全网。
- **启发式允许缺数据，正确性边界不允许**：`duration_s` / `start_lat` NULL 时模糊匹配降级（不 gate），但 `external_id sha1` 永远算到底。**两套逻辑分开设计，不混用同一套容错**。
- **测试夹具的特性会和被测功能的启发式 emergent 交互**：当这种交互暴露时，不要为测试绕过启发式，而是削窄测试意图到不依赖那层交互的断言上。测试应该验证"我声称做的那件事"，而不是"我还没做那件事会发生什么"。
- **把不确定性拆小再做决定**：Phase 3 整体一次做五件事 vs 拆成 3.1 + 3.2 延后一项有决策不确定的功能——后者让前四项可以无包袱交付，第五项等数据回来再评。拆 Phase 本身也是一种"最小化赌注面"的判断。
- **主键的语义稳定性是 `INSERT OR REPLACE` 的前提**：方便的 SQL 语法只有在底层语义 match 时才安全。Onelap activity id 真的全局唯一时，OR REPLACE 是优雅；一旦不唯一，它会吞数据。**用便捷语法前，验证语义前提**。
- **轻量 / 零依赖是用户体验的一部分**（延续 Phase 2 §7）：Phase 3.1 零新增依赖让 `uv sync` 成本不变，这是显式的设计约束——如果哪一天真的需要 WebView 或 Playwright，要把"装起来多久"放进用户体验账里，不是"反正后台装好就行"。
