# Phase 1 会话归纳：离线脚本打通链路

> 本文档沉淀了 Phase 1 从"产品构思"到"端到端跑通"的完整思考过程与关键决策，目的是让未来接手这个项目（无论是自己还是别人）的人不需要重新走一遍弯路。

## 1. 起点与目标

**起点**：一份 29 行的 [specs/product.md](../specs/product.md)，提出若干核心问题：

- 顽鹿数据怎么拿？
- Fit 怎么程序化传到 Strava？
- 为什么路线会偏移？怎么修？
- 两个平台的登录怎么处理？

**Phase 1 目标**：不做全套产品，先以**最小代价验证核心技术链路**——"本地一份 Onelap 导出的 Fit → 坐标修正 → 上传 Strava 成功"。

## 2. 关键问题回答（沉淀版）

### 2.1 为什么会偏移

中国 GPS 坐标系问题：

| 坐标系 | 谁在用 | 说明 |
| --- | --- | --- |
| WGS-84 | 全球 GPS 原始信号、国际平台（Strava / Google / Garmin） | 真实地球坐标 |
| **GCJ-02（火星坐标系）** | 国内厂商（高德、腾讯、顽鹿在内的多数国内运动 App） | 国测局强制，非线性加密偏移 |
| BD-09 | 百度 | 在 GCJ-02 基础上再加偏移 |

顽鹿 Fit 里存的是 **GCJ-02 偏移后的坐标**，Strava 按 WGS-84 解析，于是整体平移数百米。这不是简单平移，而是非线性偏移（不同位置偏移量不一样）。

### 2.2 怎么修

**GCJ-02 → WGS-84** 没有闭式解，但有公开的**正向偏移公式**（`_transform_lat` / `_transform_lng`）。反解用**不动点迭代**：

```
wgs = gcj  # 初值
for _ in range(5):
    wgs = gcj - delta(wgs)
```

偏移函数在中国区域 Lipschitz 常数很小，3~5 次迭代就能收敛到 1e-7 度（约 1cm）以内。

实现见 [src/onelap2strava/coords.py](../src/onelap2strava/coords.py)。

### 2.3 Strava 怎么对接

标准 OAuth 2.0 + 官方 API：

- 在 [strava.com/settings/api](https://www.strava.com/settings/api) 注册 App 拿 client_id/secret。Callback Domain 填 `localhost` 即可。
- 上传用 `POST /api/v3/uploads`（`stravalib` 封装好了 `client.upload_activity`）。
- 上传是**异步**的，要轮询 `uploader.wait()` 直到状态变成 Ready。
- 去重用 `external_id`（Strava 侧）+ 开始时间 ±10 分钟窗口（本地侧）两层兜底。

### 2.4 顽鹿数据怎么拿

**Phase 1 不做**，放到 Phase 2。原因：顽鹿无公开 API，需要抓包逆向私有接口，风险和工作量都不小。先用**用户手动从 App 导出 Fit** 的方式绕开，把更难的坐标修正 + Strava 对接这一段先打通。

## 3. 关键决策与取舍

### 3.1 技术栈选型

| 选择 | 理由 |
| --- | --- |
| Python 3.11+ | 坐标转换、fit 解析、stravalib 都有成熟生态 |
| `uv` | 比 pip/poetry 快，pyproject.toml 原生，现代 |
| `fit-tool` | 纯 Python，读 + 写都支持，直接返回 float 度数（省去 semicircles 手动换算） |
| 自实现坐标转换 | 约 50 行，无额外依赖；避免 `eviltransform` 等库的额外开销 |
| `stravalib` 2.x | 官方维护，OAuth + 上传轮询 + 分页查询全封装 |
| `typer` | 比 argparse 友好，自动 help；比 click 现代 |
| 标准库 `http.server` 做 OAuth 回调 | 一次性用途，没必要上 FastAPI |

### 3.2 架构取舍

**选择单文件 CLI + 模块化目录结构**，不做 FastAPI/Web：

- Phase 1 是技术验证，不是产品。越少外部依赖越好定位问题。
- 模块化（`coords.py` / `fit_fixer.py` / `strava_auth.py` / `strava_client.py` / `cli.py`）保证后续 Phase 4 Web 化时可以直接把核心模块包进 FastAPI，而不用重写。

### 3.3 测试策略的重要调整

**最初设想**：用两份 fit 按时间戳对齐，逐点 haversine 距离，断言 P95 < 5m。

**用户澄清关键信息**：两份 fit 是**同一路线的两次独立骑行**，不是同一份数据的两种表达。两条轨迹本身就有十米量级的自然差异（GPS 噪声、骑行线路）。

**重新设计测试**（重点）：

GCJ-02 偏移的**签名**是"系统性、有方向性、均值远离零的平移向量"；GPS 噪声/骑行差异的签名是"随机、无方向性、均值趋零"。据此断言：

- 修正前平均偏移向量 `|mean(dx, dy)|` > 100m（证明偏移确实存在）
- 修正后平均偏移向量 `|mean(dx, dy)|` < 30m（证明被消除）
- 改善比 > 5 倍（相对断言，避免绝对数值过拟合）

**实测结果**（远超阈值）：

| 指标 | 修正前 | 修正后 | 改善 |
| --- | --- | --- | --- |
| 平均偏移向量模长 | 221 m | 0.62 m | **356 倍** |
| P50 点距 | 306 m | 2.4 m | 128 倍 |
| P95 点距 | 475 m | 6.4 m | 74 倍 |

**教训**：测试设计前必须搞清楚夹具的来源与性质，否则断言强度会不匹配数据本身的噪声量级。

### 3.4 "不要矫枉过正"

用户两次明确强调这点：

1. 坐标转换只做反解，不引入额外"平滑"或"路网吸附"——那会引入新的偏差。
2. 测试不要求逐点一致，只要求偏移的**统计特征**改变。

这种克制很重要：GPS 数据本身有噪声，任何超过噪声量级的"修正"都是在伪造数据。

## 4. 踩过的坑

### 4.1 `fit-tool` CRC 校验

修改 fit 消息后直接 `to_file` 会抛：

```
Exception: Calculated crc (44315) != defined crc (40592)
```

原因：fit-tool 会对照原始文件里存的 CRC 和新计算的 CRC，不一致就拒写。

**解决**：写入前 `fit.crc = None`，库会重算。

[src/onelap2strava/fit_fixer.py](../src/onelap2strava/fit_fixer.py) 里保留了这行和注释。

### 4.2 坐标不只在 RecordMessage 里

顽鹿这份文件只有 `RecordMessage` 有位置字段，但**通用的 fit 文件**在 `LapMessage` / `SessionMessage` 里也可能有 `start_position_lat` / `nec_lat` / `swc_lat`（bounding box 的角点）。修正时必须覆盖这些字段，否则 Strava 显示的活动摘要位置会错。

**做法**：遍历所有消息，字段名以 `_lat` / `_long` 结尾的成对转换。不依赖消息类型，泛化性更好。注意不要误伤 `avg_left_power_phase` 这类单位也是 "degrees" 但不是地理坐标的字段——用字段名后缀过滤而不是单位过滤。

### 4.3 港澳台的边界判断

GCJ-02 标准算法的"中国范围"边界框包含港澳台。但港澳台的厂商（尤其海外接入的）实际**不对 GPS 偏移**。我们当前的选择：

- 保留标准边界框（包含港澳台）
- 在代码注释和 README FAQ 里标注这一点
- 如果用户真的在港澳台录了 fit，需要自查

这是"Onelap 场景 99% 在大陆"下的实用取舍。

### 4.4 OAuth 回调的端口

Strava 要求 Callback Domain 是域名（不能带端口），写 `localhost` 就行。**但脚本里**的 `redirect_uri` 必须带端口（`http://localhost:8000/callback`），两者不矛盾，Strava 会按 Domain 宽松匹配实际回调里的端口。

### 4.5 命令语义歧义

`upload test_data/MAGENE_C506_bias.fit` —— 输入的是**偏移的**原始 fit，用户合理怀疑是否会把错的数据传上去。

实际语义：命令是"以此文件为**源**，修正后上传"。脚本内部先 `fix_fit` 写出修正版本，再把修正版传给 Strava。

**改进**：README FAQ 第一条就解答这个疑问。如果以后觉得不够直观，可以改命令名为 `fix-and-upload`，或加 `--skip-fix` 选项明确区分。

## 5. 最终产物清单

### 代码

- [src/onelap2strava/coords.py](../src/onelap2strava/coords.py) — GCJ-02 ↔ WGS-84 互转 + in_china + haversine
- [src/onelap2strava/fit_fixer.py](../src/onelap2strava/fit_fixer.py) — fit 读 → 转 → 写，CRC 处理，元数据提取
- [src/onelap2strava/strava_auth.py](../src/onelap2strava/strava_auth.py) — 一次性 HTTPServer 接收 OAuth 回调，token 持久化与刷新
- [src/onelap2strava/strava_client.py](../src/onelap2strava/strava_client.py) — 时间窗去重，sha1 external_id，上传轮询
- [src/onelap2strava/cli.py](../src/onelap2strava/cli.py) — 5 个 typer 子命令：`auth` / `fix` / `upload` / `upload-dir` / `token-info`

### 测试

- [tests/test_coords.py](../tests/test_coords.py) — 18 个测试：回环精度、偏移量合理性、海外点 passthrough、haversine
- [tests/test_fit_fixer.py](../tests/test_fit_fixer.py) — 3 个测试：系统性偏移消除、距离分布收敛、对比打印

### 文档

- [README.md](../README.md) — 完整使用指南、FAQ、下一步
- [specs/product.md](../specs/product.md) — 原始产品构思（未改）
- [specs/roadmap.md](../specs/roadmap.md) — 四阶段演进路线图
- [contexts/phase1-offline-script.md](phase1-offline-script.md) — 本文

### 运行时生成

- `data/output/*.fixed.fit` — 修正后的 fit 文件
- `data/.strava_token.json` — OAuth token（access + refresh + expires_at）
- `.venv/` — uv 创建的虚拟环境
- `uv.lock` — 依赖锁

## 6. 验收证据

```
> uv run pytest
21 passed in 8.87s

> uv run onelap2strava fix test_data/MAGENE_C506_bias.fit
Fixed: data\output\MAGENE_C506_bias.fixed.fit
(record points converted: 3748/3748, other: 0, start: 2026-04-06 15:17:26+00:00)

> uv run onelap2strava upload test_data/MAGENE_C506_bias.fit
# 用户实机验证：Strava feed 中路线落在正确道路上
```

**Phase 1 打通标志**：Strava 上看到的骑行路线和实际道路重合，而不是平移几百米。

## 7. 对 Phase 2-4 的建议

- **Phase 2（顽鹿自动拉取）**：优先分析微信小程序而非 App；小程序 JS 包可反编译、接口签名多为明文。把"接口层"和"业务层"严格分开，因为顽鹿私有接口随时可能改版。
- **Phase 3（容错与模糊去重）**：当前 `sha1:` external_id 的弱点是顽鹿若重新导出 fit，字节会变 sha1 就变，需要改成"开始时间 ± 时间窗 + 总时长差 <5% + 起点距离 <500m"的模糊匹配。用 SQLite 记录同步历史做审计。
- **Phase 4（Web 化）**：核心代码已经模块化，FastAPI 可以直接 `from onelap2strava.fit_fixer import fix_fit`；OAuth 流程需要从本地 HTTPServer 改成 Web 路由。**强烈建议在决定走 SaaS 前慎重考虑顽鹿账号合规风险**，单用户自部署是更安全的起点。

## 8. 对话里值得保留的思维方式

- **先打通一次**：不要等完美架构，跑通一次能暴露大多数真问题。
- **不要矫枉过正**：测试不追求比数据本身噪声更严的精度；修正不引入原数据没有的"平滑"。
- **相对比较优于绝对阈值**：`fixed 改善 > 5x bias` 比 `fixed < 30m` 更稳，不会因为不同城市 GCJ 偏移量差异而误报。
- **每阶段停下来用一段时间**：避免过早优化；Phase 1 自己用一阵子，真有痛点再推进 Phase 2。
- **澄清夹具性质优先于设计测试**：两次骑行还是一次骑行两种表达，决定了测试的断言强度。
