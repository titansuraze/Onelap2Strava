# Onelap2Strava

把顽鹿运动（Onelap）导出的 Fit 文件里的 GCJ-02 偏移坐标反解为 WGS-84，再通过官方 API 上传到 Strava。

## 背景

顽鹿 App 出于合规需要，Fit 文件中的 GPS 坐标使用 **GCJ-02（火星坐标系）**，而 Strava 使用国际标准 **WGS-84**。直接把顽鹿的 Fit 丢进 Strava 会导致路线整体平移数百米，偏离真实道路。本工具就是解决这个问题。

实测效果（`test_data/` 中同一路线的两次骑行对比，见 [测试](#测试)）：

| 指标 | 修正前（顽鹿导出） | 修正后 |
| --- | --- | --- |
| 平均偏移向量模长 | **221 m**（系统性偏移，指向东略偏南） | **0.62 m** |
| 中位点距参考轨迹 | 306 m | 2.4 m |
| P95 点距参考轨迹 | 475 m | 6.4 m |

系统性偏移降低 **约 356 倍**，残留误差已经落到 GPS 噪声量级。

## 当前阶段

**Phase 1：离线脚本**（见 [specs/roadmap.md](specs/roadmap.md)）。只做三件事：

1. 读取本地 Fit 文件。
2. 将 GCJ-02 坐标转换为 WGS-84。
3. 上传到 Strava 并自动去重。

后续阶段（Onelap 自动拉取、模糊去重、Web 化）见 [roadmap](specs/roadmap.md)。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

## 配置

### 1. 注册 Strava App

到 [Strava Developers](https://www.strava.com/settings/api) 创建一个 App：

- Category: 随意（Data Importer 合适）
- Website: 随便填一个能访问的 URL（如 `http://localhost`）
- **Authorization Callback Domain**: `localhost`

创建后能看到 Client ID 和 Client Secret。

### 2. 填写 `.env`

把 `.env.example` 复制为 `.env` 并填入创建 App 时拿到的 Client ID / Secret：

```bash
# bash / git bash / macOS / Linux
cp .env.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

然后在编辑器里填入：

```
STRAVA_CLIENT_ID=<你的 Client ID>
STRAVA_CLIENT_SECRET=<你的 Client Secret>
STRAVA_REDIRECT_URI=http://localhost:8000/callback
```

## 使用

### 首次授权（只做一次）

```bash
uv run onelap2strava auth
```

浏览器会自动打开 Strava 授权页，同意后脚本自动接收 code 并保存 token 到 `data/.strava_token.json`，之后会自动刷新。

### 只做坐标修正（不上传）

```bash
uv run onelap2strava fix path/to/your.fit
```

输出到 `data/output/<原文件名>.fixed.fit`。

### 修正 + 上传（核心命令）

传入的是**原始**顽鹿 fit；脚本内部会先修正再上传，上传到 Strava 的是修正后的版本：

```bash
uv run onelap2strava upload path/to/onelap.fit
```

完整流程：

1. 把坐标修正后的 Fit 写到 `data/output/<原文件名>.fixed.fit`。
2. 按开始时间 ± 10 分钟在 Strava 查重，已有活动则跳过。
3. 上传修正后的 Fit，用 `sha1:<文件哈希>` 作为 `external_id` 做 Strava 侧兜底去重。
4. 轮询直到 Strava 处理完成，打印 activity URL。

常用选项：

- `--name "自定义活动名"`：覆盖默认名称。
- `--force`：跳过本地时间窗去重（Strava 侧依然会拒绝真正重复的上传）。

### 批量上传

```bash
uv run onelap2strava upload-dir data/input/
uv run onelap2strava upload-dir some/folder --pattern "*.fit"
```

### 查看 token 状态

```bash
uv run onelap2strava token-info
```

显示当前 token 文件路径和过期时间，首次授权后调试时有用。

## 测试

```bash
uv run pytest           # 21 个测试，约 9 秒
uv run pytest -s        # 打印夹具对比的 bias vs fixed 指标
```

包含：

- **坐标函数回环测试**（`tests/test_coords.py`，18 个）：验证 `wgs → gcj → wgs` 往返精度 < 1e-6 度、GCJ 偏移量在 100-800m 合理范围、海外点 passthrough。
- **真实夹具回归测试**（`tests/test_fit_fixer.py`，3 个）：以 `test_data/MAGENE_C506_bias.fit`（顽鹿偏移版）和 `test_data/MAGENE_C506_correct.fit`（迈金原始 WGS-84 版）为夹具，验证修正后"GCJ-02 系统性偏移被消除"。

> `test_data/*.fit` 包含真实骑行轨迹（隐私原因）不在 git 里。本地缺文件时这 3 个测试会自动 **skip** 而不是失败，坐标测试仍能跑。想运行完整回归测试，把对应文件名的 fit 放到 `test_data/`（参见 [test_data/README.md](test_data/README.md)）。

两份夹具是**同一路线的两次独立骑行**，本来就有十米量级的 GPS 噪声和骑行线路差异，因此测试目标是"**系统性偏移消除 + 距离分布落到 GPS 噪声量级**"，不追求逐点一致。核心断言：

- 修正后平均偏移向量模长 < 30 m 且相对修正前至少改善 5 倍（实测改善 356 倍）。
- 修正后 P50 距离 < 80 m 且至少是修正前的 1/3（实测 2.4 m）。

## 项目结构

```
Onelap2Strava/
├── pyproject.toml          # uv 管理的依赖
├── .env.example            # 配置模板
├── README.md               # 本文件
├── specs/
│   ├── product.md          # 产品设计文档
│   └── roadmap.md          # 四阶段演进路线图
├── src/onelap2strava/
│   ├── coords.py           # GCJ-02 ↔ WGS-84 互转
│   ├── fit_fixer.py        # Fit 读取 / 坐标修正 / 写回
│   ├── strava_auth.py      # OAuth 本地回调 + token 持久化
│   ├── strava_client.py    # 上传 + 去重 + 轮询
│   └── cli.py              # typer CLI 入口
├── tests/
│   ├── test_coords.py
│   └── test_fit_fixer.py
├── test_data/              # 测试夹具
│   ├── MAGENE_C506_bias.fit
│   └── MAGENE_C506_correct.fit
└── data/                   # 运行时数据（gitignore）
    ├── input/
    ├── output/
    └── .strava_token.json
```

## 常见问题

**Q: `upload` 命令传的是偏移的原始 fit，会不会把错的数据传上去？**
A: 不会。`upload` 的语义是"以此文件为**源**，修正后上传"。脚本内部会先调用 `fix_fit` 写出修正版本，再把修正后的文件传给 Strava。想要只修正不上传请用 `fix` 子命令。

**Q: token 会过期吗？**
A: access token 6 小时过期，脚本检测到快过期会自动用 refresh token 刷新并更新 `data/.strava_token.json`，用户无感。

**Q: 重复上传会怎样？**
A: 两层去重：本地按开始时间 ±10 分钟查 Strava 已有活动，Strava 侧再按 `external_id`（文件 sha1）兜底。想强制重试加 `--force`。

**Q: 海外骑行会被错误地平移吗？**
A: 坐标转换内置 `in_china` 粗边界判断，明显在海外（欧美澳日韩等）的点会原样 passthrough。港澳台在边界内，当前默认也会参与转换（顽鹿场景几乎只在大陆，详见 [coords.py](src/onelap2strava/coords.py) 注释）。

## 下一步

Phase 1 打通后的可能方向（按 [roadmap](specs/roadmap.md)）：

- **Phase 2**：抓包逆向顽鹿 App/小程序，去掉手动下载 Fit 这一步。
- **Phase 3**：模糊去重（时间 + 距离 + 时长）+ 失败重试 + 本地同步日志。
- **Phase 4**：FastAPI + 前端，做成 Web 服务。

建议每完成一个阶段停下来用一段时间再决定要不要做下一个。

## 许可

MIT
