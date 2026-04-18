# Onelap2Strava

一条命令把顽鹿运动（Onelap）里的骑行同步到 Strava，并在同步过程中把 GCJ-02 偏移坐标修正回 WGS-84。

## 背景

顽鹿 App 出于合规需要，Fit 文件中的 GPS 坐标使用 **GCJ-02（火星坐标系）**，Strava 使用国际标准 **WGS-84**。直接把顽鹿的 Fit 丢进 Strava 会让路线整体平移数百米、偏离真实道路。本工具解决这个问题，顺带把"从顽鹿取数据"这一步也自动化了。

实测效果（同一路线的两次骑行对比）：

| 指标 | 修正前（GCJ-02） | 修正后（WGS-84） |
| --- | --- | --- |
| 平均偏移向量模长 | **221 m**（系统性偏移，指向东略偏南） | **0.62 m** |
| 中位点距参考轨迹 | 306 m | 2.4 m |
| P95 点距参考轨迹 | 475 m | 6.4 m |

系统性偏移降低 **约 356 倍**，残留误差已经落到 GPS 噪声量级。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

## 配置 Strava

到 [Strava Developers](https://www.strava.com/settings/api) 创建一个 App：

- Category: 随意（Data Importer 合适）
- Website: 随便填一个能访问的 URL（如 `http://localhost`）
- **Authorization Callback Domain**: `localhost`

创建好 App 后，输入下方命令开始配置：

```bash
uv run onelap2strava strava-configure
```

按提示依次输入 Client ID 和 Client Secret，合法时终端将输出：

```
Verifying credentials with Strava...
[ok]    credentials accepted by Strava.
Created .env (STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REDIRECT_URI).
```

## 快速开始

**前三步只做一次**，实现一键从顽鹿导入运动数据 -> 数据修正 -> 上传 Strava：

### 1. 授权 Strava

```bash
uv run onelap2strava auth
```

浏览器会自动打开 Strava 授权页，同意后脚本接收 callback 并保存 token，之后自动刷新。

### 2. 在浏览器里打开顽鹿列表页

Chrome / Edge 直接访问 <http://u.onelap.cn/analysis/list>：

- 如果页面直接显示一段 **JSON 文本**（形如 `{"sheng":"...", "list":[...]}`），说明已经登录。
- 如果跳到登录页，正常登录后再把地址栏改回 `/analysis/list` 回车，确认能看到 JSON。

### 3. 从 DevTools 复制 Cookie 粘给脚本

仍然在 `/analysis/list` 这个页面上：

1. 按 <kbd>F12</kbd> 打开 DevTools → 切到 **Network** 标签 → 按 <kbd>F5</kbd> 刷新本页。
2. Network 列表第一条就叫 **`list`**，点它。
3. 右侧面板 → **Headers** → 往下翻到 **Request Headers** → 找到以 `Cookie:` 开头的那一行 → 选中冒号后面的**所有字符** → 复制。
4. 在终端里运行：
   ```bash
   uv run onelap2strava onelap-login
   ```
   看到 `Paste your Onelap 'Cookie' header value:` 提示后把上一步复制的内容粘进去（**终端不会回显任何字符**），回车。
5. 成功会看到：
   ```
   Saved N cookies to data/.onelap_cookies.json.
   [ok]    verified: latest activity = ...
   ```

**Cookie 能用多久？** 只要顽鹿服务端没让会话过期，就一直有效（实测至少数天 ～ 一周以上）。过期时脚本会明确提示 `cookies likely expired`——回到第 2 步刷新 `/analysis/list`，重跑第 3 步就好。

### 4. 日常同步

```bash
uv run onelap2strava sync                 # 最新 1 条
uv run onelap2strava sync --n 3           # 最新 3 条
uv run onelap2strava sync --incremental   # 自上次以来所有新骑行（推荐日常使用）
uv run onelap2strava sync --force         # 跳过所有去重，强制上传
```

## 其他命令

### 查看最近的顽鹿骑行

```bash
uv run onelap2strava onelap-list --n 5   # 只列出不上传
```

### 查看本地同步日志

```bash
uv run onelap2strava sync-log            # 最近 20 条
uv run onelap2strava sync-log --n 100
```

### 处理本地已有的 Fit 文件

如果你已经手动导出了顽鹿 Fit（或从其他渠道拿到带 GCJ-02 偏移的 Fit），可以跳过顽鹿拉取直接处理本地文件。

**只做坐标修正，不上传**：

```bash
uv run onelap2strava fix path/to/your.fit
# 输出到 data/output/<原文件名>.fixed.fit
```

**修正 + 上传**：传入的是**原始** Fit，脚本内部会先修正再上传修正后的版本：

```bash
uv run onelap2strava upload path/to/onelap.fit
uv run onelap2strava upload path/to/onelap.fit --name "自定义活动名"
uv run onelap2strava upload path/to/onelap.fit --force  # 跳过本地时间窗去重
```

**批量**：

```bash
uv run onelap2strava upload-dir data/input/
uv run onelap2strava upload-dir some/folder --pattern "*.fit"
```

### 查看 Strava token 状态

```bash
uv run onelap2strava token-info     #显示当前 token 文件路径和过期时间
```

## 常见问题

**Q: `upload` 命令传的是偏移的原始 fit，会不会把错的数据传上去？**
A: 不会。`upload` 的语义是"以此文件为**源**，修正后上传"。脚本内部会先调用 `fix_fit` 写出修正版本，再把修正后的文件传给 Strava。想要只修正不上传请用 `fix` 子命令。

**Q: Strava token 会过期吗？**
A: access token 6 小时过期，脚本检测到快过期会自动用 refresh token 刷新并更新 `data/.strava_token.json`，用户无感。

**Q: 重复上传会怎样？**
A: 三层去重：
1. **本地模糊去重**（Phase 3.1）——本地 SQLite 日志按"开始时间 ±10min + 时长差 <5% + 起点 <500m"三元组匹配，命中即跳过，不触达 Strava。主要防顽鹿重新导出同一骑行（字节不同但内容几乎一致）的情况。
2. **Strava 时间窗**——命中本地之后，Strava 侧再按 `get_activities ±10min` 兜底。
3. **Strava external_id**——最终按文件 sha1 的 `external_id` 做服务端去重。

三层都想跳过用 `--force`。

**Q: 海外骑行会被错误地平移吗？**
A: 坐标转换内置 `in_china` 粗边界判断，明显在海外（欧美澳日韩等）的点会原样 passthrough。港澳台在边界内，当前默认也会参与转换（顽鹿场景几乎只在大陆，详见 [coords.py](src/onelap2strava/coords.py) 注释）。

**Q: 顽鹿 Cookie 需要每次使用都获取吗？**
A: 不需要。`onelap-login` 跑一次后存到本地 `data/.onelap_cookies.json`，之后 `sync` 直接读。实测一次能用数天到一周以上。过期时脚本明确报 `cookies likely expired`，重跑 `onelap-login` 即可。

**Q: 第一次跑 `sync` 会不会把 `data/cache/` 里已有的 Fit 当成"新同步"再传一遍？**
A: 不会。Phase 3.1 引入 SQLite 日志时会自动扫描 `data/cache/*.fit` 做一次 backfill（status=`backfilled`），之后所有 `sync` 的模糊去重都能识别这些历史骑行。看到日志行 `backfilled N cached fits into sync log` 就是触发了。backfill 只在日志为空（第一次启用或被删除）时跑一次，不会重复扫描。

**Q: 上传失败会自动重试吗？**
A: 会——但只对"瞬时"错误。网络连接错误（`ConnectionError`）、超时（`Timeout`）、分块编码中断等视为瞬时，按 1s/2s/4s 指数退避重试最多 3 次。鉴权错误、参数错误、文件不存在这类"重试也没用"的立即失败，错误信息落在 `data/.sync.db` 里（status=`failed`），下次 `sync` 会再试一次。

**Q: 粘贴 Cookie 时终端看起来没反应是不是 bug？**
A: 不是。`onelap-login` 交互式 prompt 故意关掉了输入回显（Cookie 里带敏感 token），粘贴时看起来像没动——直接回车就好，脚本会立刻解析并把结果打出来。如果不喜欢这个行为可以走 `--cookie "<值>"` 非交互形式。

**Q: 从 DevTools 复制 Cookie 时最容易踩的坑？**
A: 在 `/analysis` 页面（或任何嵌入了百度埋点的顽鹿页面）里随便挑请求复制 Cookie，大概率会选到 `miao.baidu.com` / `hm.baidu.com` 这类第三方埋点——从那复制的 Cookie 全是百度域的（`BAIDUID_BFESS` / `ab_jid` ...），粘回来完全无效。**标准做法是直接访问 `http://u.onelap.cn/analysis/list`**，这是纯 JSON 接口，Network 里名字叫 `list` 的第一条就是它，不会混入任何第三方请求。

**Q: 为什么不自动读浏览器 Cookie？**
A: 曾经实现过（`--from-browser` 选项 + `browser-cookie3` 依赖）。Chrome / Edge 125+ 的 App-Bound Encryption 让这条路径在 Windows 上极不稳定——有时 `[Errno 13] Permission denied`，有时 `Unable to get key for cookie decryption`，以管理员身份也未必解得开，不同机器行为差异很大。维护"看起来方便实际时灵时不灵"的路径比让用户手粘一次体验更糟，已经完全下线。完整决策过程见 [contexts/phase2-onelap-scraping.md](contexts/phase2-onelap-scraping.md)。

**Q: `onelap-list` 能用但 `sync` 报错怎么排查？**
A: `onelap-list` 只拿列表，`sync` 还要下载 Fit 和上传 Strava。如果 `onelap-list` OK，问题多半在后两步：
- **下载环节**：`data/cache/` 下是否有半截的 `.fit.part`？（正常情况下成功写入后会原子 rename 掉，有的话说明前次网络中断。）
- **上传环节**：跑 `uv run onelap2strava token-info` 看 Strava token 还活着吗？过期自动刷新，但如果 refresh token 也失效要重跑 `auth`。

## 项目结构

```
Onelap2Strava/
├── pyproject.toml          # uv 管理的依赖
├── README.md               # 本文件
├── specs/
│   ├── product.md          # 产品设计文档
│   └── roadmap.md          # 演进路线图
├── contexts/               # 历次迭代的决策归纳（供参考，不影响使用）
│   ├── phase1-offline-script.md
│   ├── phase2-onelap-api.md                # 顽鹿接口清单 + 侦察指引
│   ├── phase2-onelap-scraping.md           # Phase 2 决策演进（含 --from-browser 的退场）
│   └── phase3.1-dedupe-and-resilience.md   # Phase 3.1 决策演进（SQLite / 模糊去重 / 重试 / 增量）
├── src/onelap2strava/
│   ├── coords.py           # GCJ-02 ↔ WGS-84 互转 + Haversine
│   ├── fit_fixer.py        # Fit 读取 / 坐标修正 / 写回 + read_fit_metadata
│   ├── strava_auth.py      # Strava OAuth 本地回调 + token 持久化
│   ├── strava_client.py    # Strava 上传 + 去重 + 轮询
│   ├── sync.py             # 主编排：onelap 拉 → 模糊去重 → fix → 重试上传 → 落日志
│   ├── sync_log.py         # 本地 SQLite 同步日志（模糊去重 / 增量 / backfill）
│   ├── onelap/             # 顽鹿接口层（接口改版时只改这里）
│   │   ├── client.py       # HTTP + 列表/下载 + 会话过期识别
│   │   ├── auth.py         # Cookie 持久化与加载
│   │   └── models.py       # Activity 数据类
│   └── cli.py              # typer CLI 入口
├── tests/
│   ├── test_cli.py
│   ├── test_coords.py
│   ├── test_fit_fixer.py
│   ├── test_onelap_client.py
│   ├── test_sync.py
│   └── test_sync_log.py
├── test_data/              # 测试夹具
│   ├── MAGENE_C506_bias.fit
│   └── MAGENE_C506_correct.fit
└── data/                   # 运行时数据（gitignore）
    ├── input/
    ├── output/             # 修正后的 fit
    ├── cache/              # 顽鹿下载的原始 fit
    ├── .strava_token.json  # Strava OAuth token
    ├── .onelap_cookies.json # 顽鹿会话 Cookie
    └── .sync.db            # Phase 3.1 同步日志（SQLite）
```

## 测试

```bash
uv run pytest           # 72 个测试，约 60 秒
uv run pytest -s        # 打印夹具对比的 bias vs fixed 指标
```

包含：

- **坐标函数回环测试**（`tests/test_coords.py`，18 个）：验证 `wgs → gcj → wgs` 往返精度 < 1e-6 度、GCJ 偏移量在 100-800m 合理范围、海外点 passthrough。
- **真实夹具回归测试**（`tests/test_fit_fixer.py`，4 个）：以 `test_data/MAGENE_C506_bias.fit`（顽鹿偏移版）和 `test_data/MAGENE_C506_correct.fit`（迈金原始 WGS-84 版）为夹具，验证修正后"GCJ-02 系统性偏移被消除"；Phase 3.1 加入 `read_fit_metadata` 对真实 Fit 的断言。
- **顽鹿接口层单测**（`tests/test_onelap_client.py`，17 个）：用 `responses` mock HTTP，覆盖 Cookie 解析、列表解析排序、限量、会话过期（HTML 响应 / 401）识别、下载流式写入与缓存命中。
- **同步流水线 mock 测试**（`tests/test_sync.py`，11 个）：端到端用假 Onelap + 假 Strava 跑通"拉 → 修 → 传"，覆盖 Strava 时间窗去重、单条失败不阻塞、Phase 3.1 的模糊去重命中、`--force` 绕过、重试成功/耗尽、非瞬时错误不重试、`--incremental` 过滤 seen id、首次启用 backfill。
- **同步日志单测**（`tests/test_sync_log.py`，17 个）：schema 幂等、三元组边界（时间窗 / 时长比 / 起点距离）、failed 不混入模糊去重、backfilled 参与模糊去重但不污染增量 seen 集、backfill 对空目录 / 真 fit / 解析失败的处理。
- **CLI 契约测试**（`tests/test_cli.py`，5 个）：`--incremental` 和 `--n` 互斥校验、`sync-log` 子命令的空/有数据路径、参数穿透到 `run_sync`。

> `test_data/*.fit` 包含真实骑行轨迹（隐私原因）不在 git 里。本地缺文件时这 3 个测试会自动 **skip** 而不是失败，坐标测试仍能跑。想运行完整回归测试，把对应文件名的 fit 放到 `test_data/`（参见 [test_data/README.md](test_data/README.md)）。

两份夹具是**同一路线的两次独立骑行**，本来就有十米量级的 GPS 噪声和骑行线路差异，因此测试目标是"**系统性偏移消除 + 距离分布落到 GPS 噪声量级**"，不追求逐点一致。核心断言：

- 修正后平均偏移向量模长 < 30 m 且相对修正前至少改善 5 倍（实测改善 356 倍）。
- 修正后 P50 距离 < 80 m 且至少是修正前的 1/3（实测 2.4 m）。

## 路线图

可能的演进方向（详见 [specs/roadmap.md](specs/roadmap.md)）：

- **Phase 3.1 ✅**：本地 SQLite 同步日志 + 模糊去重（时间 ± 时长 + 起点三元组）+ 失败重试（指数退避）+ 增量同步。已交付。
- **Phase 3.2 📋 Cookie 续期**：目前过期要手动跑两步（浏览器刷新 + 重跑 `onelap-login`）。可以考虑让 `sync` 检测到过期时拉起一个迷你 WebView 直接复用用户已登录的浏览器 session，规避 ABE 限制。待评估 WebView 的安装摩擦是否真比手粘一次更小。
- **Phase 4 💡 Web 服务化**：FastAPI + 前端，多账号、定时同步。

## 许可

MIT
