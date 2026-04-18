# Phase 2 会话归纳：自动化顽鹿数据拉取

> 本文档沉淀了 Phase 2 从"规划"到"端到端可用"的关键决策，延续 [phase1-offline-script.md](phase1-offline-script.md) 的风格。重点记录**为什么没按 roadmap 的 mitmproxy 路径走，以及为什么这样反而更稳**。

## 1. 起点与目标

**起点**：Phase 1 已打通"手动导出 Fit → 修正 → 上传 Strava"整条链路，用户手动导出这一步依然是 UX 里最刺眼的环节。

**Phase 2 目标**：让 `uv run onelap2strava sync` 一条命令完成"拉最新 Fit → 修坐标 → 上传 Strava"，用户不再接触 Fit 文件。

## 2. 关键决策与取舍

### 2.1 侦察策略：从"抓包逆向 App"改为"公开项目 + DevTools"

**原计划（roadmap）**：mitmproxy/Charles 抓微信小程序或 App 的 HTTPS，逆向登录/列表/下载三个接口。

**实际发现**：开源项目 [`moruoxian/SyncOnelapToXoss`](https://github.com/moruoxian/SyncOnelapToXoss) 已经做过一轮（支持 OneLap → Strava 同步），并且顽鹿**有 Web 端**（`u.onelap.cn/analysis`）。

**决策**：直接基于 Web 端 + 已知开源实现，不走手机抓包。收益：

- 省掉 mitmproxy 装证书的 2-4 小时。
- Web 端 Cookie-based Session 比 App 的自研签名体系简单一个数量级。
- DevTools 的 "Copy as cURL" 天然可复现，不需要再另写抓包报告。

**代价**：顽鹿 App 里可能有 Web 端没有的接口（比如实时数据推送）。**对 Phase 2 目标来说不需要**，所以这个代价是可接受的。

把"mitmproxy 手机抓包"降级为"Phase 2 TODO"记在了 [phase2-onelap-api.md §5](phase2-onelap-api.md)。

### 2.2 登录策略：从"密码登录"改为"手粘 Cookie"

**用户选择**（开场问卷）：App 手机号+密码登录。

**理想实现**：`POST https://www.onelap.cn/api/login` 传 `username + MD5(password)`，拿 Set-Cookie。这条路径在若干 CSDN 博客里被提到，但**没有任何一手验证**（博主可能没更新，也可能顽鹿已改）。

**实际风险**：如果硬编码一个未验证的登录 API：

- 成功了还好；失败了用户拿到的报错是"password incorrect"还是"account locked"？
- 接口一旦改版（这是顽鹿私有接口，随时可能），我们的登录就挂，重新侦察又要一轮。

**最终选择**：不实现自动登录，把登录 UX 简化为"**手粘 Cookie**"：

1. 用户在浏览器登录 → 开 DevTools → 复制 Request Headers 里的 `Cookie` 串。
2. `uv run onelap2strava onelap-login` 粘贴一次。
3. 之后 `sync` 直接复用，Cookie 过期时再粘一次。

这个方案的好处：

- **对顽鹿登录实现完全免疫**。无论顽鹿是纯密码、还是加了滑块、还是改成扫码，浏览器总能登成功，我们总能拿到 Cookie。
- Cookie 过期周期长（实测几天到一周），日常使用频率几乎零。
- 代码面只增加一个"**Cookie 字符串解析 + 持久化**"的薄层（[`onelap/auth.py`](../src/onelap2strava/onelap/auth.py)），不引入任何对顽鹿登录实现的假设。

roadmap 里写的"keyring 加密存 password"也因此不再需要——我们根本不存密码。

### 2.3 会话过期识别：不是 401 而是 200 + HTML

**踩坑**：以为 Cookie 过期会返回 401/403，初稿的 `_raise_if_auth_required` 只检查状态码。

**参考项目代码 + 常识**：顽鹿的 Web 层是传统 server-rendered 站点混一层 JSON API，Session 过期时的典型行为是"**跳转到登录页**"，也就是 `200 OK + Content-Type: text/html`。

**修正**：在接入层（`client.py::_raise_if_auth_required`）同时检查 "401/403 原生" 和 "200 + text/html"，任一命中都抛 `OnelapAuthRequired`，CLI 捕获后给出清晰的"请重跑 onelap-login"提示。

这个双重识别也在 `test_onelap_client.py::test_list_activities_html_response_raises_auth_required` 里单独断言。

### 2.4 接口层严格隔离

复用了 Phase 1 的模块哲学：

- 所有 onelap.cn 的访问代码都在 `src/onelap2strava/onelap/` 下，**其他模块一概不 import `requests`**。
- 字段映射集中在 `models.py::Activity.from_api`，raw payload 里每个字段改名，只影响这一个方法。
- 端点 URL 集中在 `client.py` 模块常量里（`BASE_URL_U` / `PATH_LIST`），未来顽鹿换域名只改一行。

这样维护成本几乎全集中在一个目录，跟 roadmap 要求的"隔离接口层便于维护"一致。

### 2.5 缓存而不只是临时文件

**设计**：下载的原始 Fit 落到 `data/cache/<filename>`，而不是直接 pipeline 到 fix_fit 的内存流。

**理由**：

- **幂等**：同一活动重复跑 `sync` 不会重新下载。
- **审计**：失败时能对比"我下载到的"和"Onelap 现在显示的"。
- **断点续跑**：上传 Strava 失败可以跳过下载直接重试。
- 代价是 KB ~ 几 MB 一个的 fit 文件堆在磁盘，远不如下载省流量重要。

Phase 3 加 SQLite 同步日志时这个缓存就是天然的"已处理记录"对照表。

### 2.6 登录策略 v2：从"手粘唯一"到"浏览器自动 + 手粘兜底"

**触发**：主线 Phase 2 完成后，用户抛了一个看似简单但其实触碰架构的问题——"Cookie 位置既然明确，能不能让脚本自动获取，而不是手动复制？"

**三条可选路径**：

| 路线 | 原理 | 工作量 | 隐含依赖 |
| --- | --- | --- | --- |
| A. 读浏览器本地 Cookie DB | Chromium 在 SQLite + DPAPI 存 Cookie；用现成库 `browser-cookie3` 解密 | ~100 行 + 一个 optional extra | 桌面浏览器；Chrome 125+ ABE 加密可能阻止 |
| B. 嵌入无头浏览器跑登录 | 起 Chromium，用户在里面登录，脚本捕获 Cookie | 几百行 + Chromium 二进制 | 用户仍要手动登录，跟现状没本质区别 |
| C. 浏览器扩展 + native messaging | 装我们写的扩展一键推 Cookie 给本地脚本 | Phase 4 量级 | 要在 Chrome Web Store 发扩展 |

显然 A 是唯一有增量价值的选项，B/C 被排除。

**关键拒绝诱惑**：有一瞬间想把方案 A 做成"唯一路径"，让 `onelap-login` 默认就读浏览器、删掉手粘逻辑。阻止这么做的是一个清醒判断：

> Chrome 125+ 于 2024 引入了 App-Bound Encryption (ABE)，让第三方进程读 Cookie 的门槛显著抬高。`browser-cookie3` 追赶中但**不保证**每个 Chrome 小版本都能成功。如果我们把方案 A 做成唯一入口，下一次用户 Chrome 自动升级就可能让 `sync` 彻底失灵。

**最终设计：加一条路径，不换路径**。`onelap-login` 接受 `--from-browser [auto|edge|chrome|...]` 作为推荐默认，交互粘贴永远作为 fallback 保留。直接好处：

- ABE 或 DB 锁失败时，`BrowserCookieReadError` 的错误消息主动引导用户回到手粘流程，不会卡死。
- 方案 A 的第三方依赖（`browser-cookie3` + `pycryptodomex` + `lz4`）放到 `[project.optional-dependencies].browser` extra；默认 `uv sync` 装的基础 CLI 依然轻量，只有主动选方案 A 的用户才 `uv sync --extra browser`。
- `browser_cookies.py` 里对 `browser_cookie3` 的 import 是**延迟的**——没装 extra 的用户跑其他子命令不会 ImportError。

这套设计的元结构是"**在不稳定的第三方能力上做 graceful degradation**"：`browser-cookie3` 是我们**希望**能工作的东西，不是**必须**能工作的东西。主路径保留一条永不依赖第三方能力的备胎，才敢把新路径推成默认。

**附带的三层漏斗**（在 `cli.py::onelap_login` 里）是对同一思路的 runtime 表达：

- **L1** 能否读浏览器 Cookie DB → 失败规范化为 `BrowserCookieReadError`，CLI 给出"改走手粘"的提示。
- **L2** 读到的 Cookie 看起来像不像登录态（见 §3.5）→ 软警告，不阻断。
- **L3** 真的打一次 `/analysis/list`，服务端返回 JSON 才算通过（与 §2.3 同一路径）。

L1/L2 是 UX 层的 early-fail 优化，L3 是正确性边界。方案 A 和方案 B 共享 L3——它才是唯一的权威裁判。

### 2.7 登录策略 v3：收回 `--from-browser`，手粘变回唯一路径

**触发**：v2 上线后用户在真实 Windows + Edge 147 机器上实测，错误接连来了四轮：

| 轮次 | 错误文本 | 根因 |
| --- | --- | --- |
| 1 | `[Errno 13] Permission denied: 'C:\Users\<user>/cookies'` | `browser_cookie3.load()` 顺带探测 `w3m`/`lynx`，Windows 上 `~/cookies` 不存在 → 误报权限错误 |
| 2 | `This operation requires admin. Please run as admin.` | Chrome/Edge 125+ ABE（App-Bound Encryption），DPAPI 解密需要 SYSTEM/admin |
| 3 | `Unable to get key for cookie decryption` | 同样是 ABE，只是 `browser-cookie3` 某个小版本换了措辞，绕过了初版的关键字分类器 |
| 4 | （以管理员身份重跑）同第 3 条 | ABE 在这台机器上**即便提权也解不开密钥** |

前三轮每一个错误我都有对应的缓解：自己实现 `_load_auto` 白名单不再踩 `~/cookies`；分类器识别 `admin` 关键字引导提权；识别 `unable to get key` / `cookie decryption` 补齐 ABE 变体；把"没装的浏览器"降级为括注而不是等权重错误。**每一步改完，错误消息本身变得更好，但问题没消失**——第四轮实测揭示了真正的边界：这台机器上 `browser-cookie3` 对 ABE 就是没有稳定的解决方案。

继续挖坑的性价比此时彻底崩塌。只有两种路径能把 ABE 跑通：

- **等 `browser-cookie3` 上游跟进**（但其实已经等了一段，且不同 Chrome 小版本还会继续漂移）；
- **自己实现 DPAPI + ABE 的 key unsealing**（工作量是四位数行，而且每个 Chrome 版本换 key derivation 都要重追）。

对一个"一次手粘就能绕过、且手粘 Cookie 能用一周以上"的场景来说，**这两个都是巨亏的投入**。

**最终决定：完全下线 `--from-browser`**。删掉的东西：

- `src/onelap2strava/onelap/browser_cookies.py`（~170 行 + 24 个测试）
- `[project.optional-dependencies].browser = ["browser-cookie3>=0.19"]` 及其拖进来的 7 个二进制依赖（`pycryptodomex` / `lz4` / `pywin32` / `shadowcopy` / `wmi` / `browser-cookie3` 本体 / `pywin32-ctypes`）
- CLI 的 `--from-browser` 选项、L1/L2 漏斗、ABE/DB 锁的错误分类器
- README/FAQ/context 里所有推荐 `--from-browser` 的措辞

保留的东西：

- 手粘交互 prompt + `--cookie` 非交互参数（v1 就有，始终是正确性边界）
- L3 live probe（方案 A 和 B 本来就共享，现在是唯一需要的那一层）

**这段经历沉淀下来的原则**（加到 §7）：

1. **"加一条路径"的承诺是双向的**：§2.6 强调"加一条路径不要换一条路径"是正确的（因此我们从未把手粘删掉）。但"加"一条新路径也带着一个隐含义务——**如果新路径在现实里反复证伪，也要有勇气把它撤掉**。留着一条"有 20% 概率能用"的路径不如没有：它会让用户对默认推荐产生不信任，污染整个 onboarding 体验。
2. **错误消息好看 ≠ 问题解决**：前三轮每一次改进错误分类，都是在优化一个**糟糕路径的失败体验**。只有第四轮让我们看清"这条路径本身不可靠"——这是两件完全不同的事。工程里区分"让问题的现象更友好"和"让问题不再出现"是第一等重要的品味。
3. **Phase 2 的 L3 live probe 是这次决定能干脆的原因**：因为手粘路径早就有一次权威的服务端校验，我们有信心说"删掉方案 A 不会让产品变脆弱"——L3 仍然兜住所有可能的 Cookie 错误。**好的架构给你撤销决定的自由**。如果当初没有 L3，撤销会很痛，因为你不知道新入口会不会把坏 Cookie 静默写进磁盘。

**历史教训保留**：§2.6 没有被删，它记录了"我们为什么加了这条路径"。§2.7 记录了"我们为什么撤掉了它"。两段放一起看才能把完整的判断链交给下一个读这份文档的人——只剩后半段会让人误以为手粘是从第一天起就是显然的选择，但实际上它是走完方案 A 并实测证伪之后、才重新被确认为**唯一稳的路径**。

## 3. 踩过的坑（或避开的坑）

### 3.1 Cookie 键名硬编码的诱惑（以及实测对此的 validation）

**设计时的判断**：一开始打算只保留 `PHPSESSID` 和 `access_token` 两个"经典"鉴权 Cookie，其它丢掉。最终选的是**全量透传**——解析 Cookie 串时保留所有键值对，发请求时全部带上。

**实测阶段意外验证了这个选择**：用户登录后浏览器里发到 `u.onelap.cn` 的 Cookie 里根本**没有** `PHPSESSID` 或 `access_token`，真实业务键是：

- `OTOKEN`（推测：Onelap Token 的缩写，Base64 + JSON 封装）
- `XSRF-TOKEN`（CSRF 防御 token）
- `ouid`（用户 ID）

如果当初按 CSDN 博客的猜测过滤，过滤器返回空 dict，请求必然失败。全量透传让我们对"博客事实错误"这种常见噪声完全免疫。

**这件事的真实分量**超出了 Cookie 本身——它是一个推理链：

> 最基础的 Cookie 键都被二手信息写错了 → 同一批博客写的 `/api/login` 请求体结构大概率也错 → 把"未验证的二手信息"留在用户交互里（手粘 Cookie）而不是代码里，是对这种系统性不确定性的正确兜底。

换个说法：**私有接口的二手信息带有一个不可观测的错误率**，任何依赖这些信息的实现都要为此留余量。"全量透传"和"不自动登录"是同一个原则在两个层面的表达。

### 3.2 从"过滤关键字"到"直接访问 JSON 端点"的演进

文档第一版写的是"在 `/analysis` 页面里过滤 `onelap` 找骑行列表接口"——看起来合理，实际上两层坑：

**坑 1（发现于首次实测）**：过滤 `onelap` 会匹到 `miao.baidu.com` 的请求。顽鹿页面嵌入了百度埋点，它们的 URL 里带了 query 参数 `_o=https%3A%2F%2Fu.onelap.cn%2F...`——filter 是 substring 匹配，于是 "onelap" 命中到了百度请求的 URL 里。从那复制的 Cookie 全是百度域的（`BAIDUID_BFESS` / `ab_jid` / `H_WISE_SIDS_BFESS` ...），粘回来完全无效。

第一轮修正：把过滤关键字从 `onelap` 改成 `analysis/list`。但这依然需要用户**理解**过滤语法、**认得**哪条请求是想要的。

**坑 2（用户进一步反馈）**：既然真正需要的就是 `/analysis/list` 这个接口，为什么不让用户**直接访问它**？

**最终方案**：让用户地址栏直接敲 `http://u.onelap.cn/analysis/list`，它是纯 JSON 接口，有效登录态下浏览器会显示一段 JSON 文本。Network 里就一条名字叫 `list` 的 document 请求（就是当前页面本身，永远在最上面），**不存在"挑哪条"的问题**。

从"文档工作量"和"用户工作量"看：

| 版本 | 用户的认知负担 | 可能出错的环节 |
| --- | --- | --- |
| v1：`/analysis` 页面过滤 `onelap` | 理解过滤框、在几十条请求里辨认 | Host 是否正确、是不是百度埋点、过滤语法 |
| v2：`/analysis` 页面过滤 `analysis/list` | 同上但关键字更精确 | 仍要辨认，且需要用户懂 URL 片段 |
| v3：直接访问 `/analysis/list` | 只需认得"最上面那条 list" | **几乎不可能错** |

**教训**：好的文档指令不是"把已知事实说给用户听"，而是"**选一条让用户不可能走错的路径**"。第一版的我知道 list 接口在哪、哪些是百度请求，所以随便挑一条都对；第一次看的用户没有这些先验，只好听从字面。**把文档和首次用户的知识差视为不可修复的系统性误差**——我们的工作是通过改流程来闭合这个差。

### 3.3 `download_fit` 的原子性

下载中途网络挂掉会留下半截 .fit，下次 `sync` 会把它当"已缓存"跳过下载——于是永远处理不了一个损坏文件。

**修正**：先写 `xxx.fit.part`，成功后 `Path.replace` 原子改名。部分下载的文件永远不会以 `.fit` 结尾被 "cache hit" 误认。

### 3.4 测试不打真实网络

用 `responses` 库把 `requests` 的 socket 层彻底截断（装的时候是开发依赖）。任何漏 mock 的请求都会直接 `ConnectionError`，反过来帮我们确认"cache hit 那条路径真的没打网络"这种正向断言。

对 Strava 侧用 `unittest.mock.MagicMock` 按 `upload_fit` 实际调用的两个方法（`get_activities` / `upload_activity`）写假对象——不引入额外依赖。

### 3.5 L2 启发式用通用键名而不是顽鹿特定键名

`browser_cookies.looks_like_logged_in` 的实现要判断"读到的这堆 Cookie 看起来像登录态吗"。最自然的写法是硬编码顽鹿已知业务键：

```python
# 诱惑版本（别这么写）
_AUTH_KEYS = {"OTOKEN", "XSRF-TOKEN", "ouid"}
return any(k in _AUTH_KEYS for k in cookies)
```

但这正是 §3.1 那个坑的翻版——我们对博客猜 `PHPSESSID` 嗤之以鼻，然后对实测得来的 `OTOKEN` 硬编码，不过是把"谁犯错"从博主换成了将来的自己。顽鹿下次改字段，我们这里就挂。

**实际实现**：用通用 Web 行业关键字做 substring 软匹配：

```python
_LIKELY_AUTH_KEY_SUBSTRINGS = ("token", "session", "sid", "ouid", "uid", "auth")
```

顽鹿把 `OTOKEN` 换成 `AUTH_TOKEN` 或 `SESSIONID`，启发式仍然通过。**启发式对命名风格的假设要尽量贴近行业共识而不是特定站点的当前快照**——前者相对稳定，后者随时漂移。

更重要的：启发式本来就**不是正确性边界**。即使命中错误（误判为非登录态，或漏判空壳 Cookie 为登录态），L3 活体探针永远是最终裁判。启发式的整个意义只是让"根本没登录"这种最常见的失败在**网络请求发出之前**被拦住，给用户更精准的错误消息（"你还没登录" vs "你登录了但 Cookie 过期了"）。

## 4. 最终产物清单

> Phase 2 经历了 v1（手粘）→ v2（手粘 + 浏览器自动）→ v3（撤回浏览器自动，手粘唯一）的演进。以下是 **v3 终态**的产物清单。v2 阶段的 `browser_cookies.py` / `[browser]` extra / `--from-browser` 选项等都已删除，但决策路径完整保留在 §2.6 + §2.7 里。

### 代码

- [src/onelap2strava/onelap/client.py](../src/onelap2strava/onelap/client.py) — OnelapClient：HTTP 会话、列表、下载、会话过期识别（HTML/401 双识别）
- [src/onelap2strava/onelap/auth.py](../src/onelap2strava/onelap/auth.py) — Cookie 解析、持久化、加载、认证客户端工厂
- [src/onelap2strava/onelap/models.py](../src/onelap2strava/onelap/models.py) — Activity 数据类
- [src/onelap2strava/sync.py](../src/onelap2strava/sync.py) — run_sync 编排层 + 报表
- [src/onelap2strava/cli.py](../src/onelap2strava/cli.py) — 新增 `onelap-login` / `onelap-list` / `sync` 三个子命令；`onelap-login` 做一次 live probe 作为正确性边界

### 配置

- [pyproject.toml](../pyproject.toml) — 依赖保持 Phase 1 水准的轻量，只有 `dev` 一个 optional-dependencies 组（装 `pytest` + `responses`）

### 测试

- [tests/test_onelap_client.py](../tests/test_onelap_client.py) — 17 个测试：Cookie 解析、列表解析排序/限量、HTML/401 识别、下载缓存/流式/过期
- [tests/test_sync.py](../tests/test_sync.py) — 3 个测试：端到端 mock 路径、去重跳过路径、单条失败不阻塞其它

### 文档

- [contexts/phase2-onelap-api.md](phase2-onelap-api.md) — 接口清单 + 手粘 Cookie 流程 + 侦察指引 + 契约 + 已知限制（含 ABE 不可靠性）+ TODO
- [contexts/phase2-onelap-scraping.md](phase2-onelap-scraping.md) — 本文（含 v2 → v3 回退决策）
- [README.md](../README.md) — "快速开始"直接指引手粘 Cookie 流程

### 运行时生成

- `data/.onelap_cookies.json` — Cookie 持久化
- `data/cache/*.fit` — 顽鹿下载的原始 Fit（GCJ-02 偏移版）

## 5. 验收证据

```
> uv run pytest
41 passed in ~17s   (Phase 1 的 21 + Phase 2 的 20)

> uv run onelap2strava --help
# 8 个子命令：auth / fix / upload / upload-dir / onelap-login / onelap-list / sync / token-info

> uv run onelap2strava onelap-login --help
# 只剩 --cookie 一个可选参数；不带参数进入交互 prompt（hide_input=True）
```

**Phase 2 打通标志**：用户执行 `onelap-login`（一次手粘，几天到一周一次）+ `sync`（日常），就把最新骑行从顽鹿同步到了 Strava，路线位置正确（复用 Phase 1 的坐标修正）。

## 6. 对 Phase 3 的建议

- **SQLite 同步日志**：Phase 2 的 `data/cache/` 已经是"处理过的 fit"的天然记录，加一张 SQLite 表（Fit hash + Strava activity id + 同步时间）就能支撑模糊去重和审计。
- **模糊去重**：比 Phase 1 的"时间 ±10 分钟 + external_id sha1"更强——顽鹿可能重新导出同一次骑行，字节不同但内容几乎一致。加"总时长差 <5% + 起点距离 <500m"的三元组判定。
- **Cookie 过期时的流畅续期**：v2 的 `--from-browser` 已放弃（见 §2.7），但 Cookie 过期要手动跑两步的摩擦仍在。真要做自动续期，路线应该是**嵌入一个 WebView 复用用户的浏览器 session**（走 Playwright / pywebview 的 `storage_state`），而不是再试一次"第三方解密 Cookie 数据库"——前者规避 ABE 的根本边界（DPAPI 密钥不在我们手里）。决策时先评估 WebView 在目标平台的安装摩擦是否真比手粘一次更小。
- **API 登录（如果抓包确认）**：原本 roadmap 有这条；v3 终态下它的相对价值回升——如果顽鹿有稳定的 `/api/login`，就能完全绕过浏览器。但前提仍然是一手抓包确认，不能依赖 CSDN 博客的二手信息。
- **失败重试**：当前单条失败不阻塞其它，但没重试；`sync.py::_sync_one` 外层加指数退避即可。

## 7. 对话里值得保留的思维方式

- **不信的信息不写进代码**：CSDN 博客提到的 `/api/login` 虽然合理但未验证，所以完全不碰，把风险留给用户一次手动操作（粘 Cookie）。这比"实现个可能挂的东西"稳。实测阶段验证了同样的博客把关键 Cookie 键名也写错了（说是 `PHPSESSID`，实际是 `OTOKEN`），追加一条佐证。
- **侦察优先找肩膀站**：mitmproxy 是个锤子，但如果已有公开实现做了同样的事，直接读别人的代码比自己从 HTTPS 明文里扒一遍接口省 10 倍时间。
- **对私有接口的假设最小化**：接入层不假设 Cookie 键名、不假设鉴权方式、不假设响应字段全集。变了就改一个地方，不变就什么都不用动。
- **缓存、原子写、全量透传**：三个小工程习惯合起来让 Phase 2 的 30 行代码比粗糙的 300 行更稳。
- **文档里让人走错的那一步要精确到消除歧义**：用户跟着"过滤 onelap"这种泛指令，真的会掉进百度埋点请求的坑；把指令改成"过滤 `analysis/list`"+ "确认 Host 是 u.onelap.cn"后才真正不会踩。**写用户文档**和**写代码注释**对同一件事的精度要求完全不同。
- **加一条路径，不要换一条路径——但也要敢撤掉证伪的路径**：§2.6 加了 `--from-browser` 作为推荐默认，手粘作为 fallback；§2.7 实测证明 ABE 让方案 A 在 Windows 上不可靠，于是撤回推荐、删掉代码，只留手粘。加路径容易，**撤路径需要额外的勇气**——因为它意味着承认之前那部分投入是沉没成本。但留着一条"20% 几率能用"的路径对用户体验是净负收益。§2.2 "不做自动登录"和 §2.7 "撤掉浏览器自动" 是同一判断在不同时点的两次应用。
- **错误消息好看 ≠ 问题解决**：为"糟糕的失败路径"持续优化错误分类是危险的，因为每一轮改进都让你误以为离解决更近；但**现象的友好程度**和**原因是否消除**是两件事。当分类器已经追赶到"所有观测到的 ABE 变体都识别出来"时，仍然要问：这条路径**本质上**可靠吗？不可靠就撤，不管错误消息打磨得多漂亮。
- **好的架构给你撤销决定的自由**：Phase 2 的 live probe (L3) 是方案 A 和手粘共享的权威裁判——正因为有它兜底，我们才敢直接删掉方案 A 而不怕产品变脆弱。**凡是引入新入口的设计，都应该在决定之前想清楚撤回成本**；如果撤回会让现有能力的正确性保证变差，就不是可以自由实验的路径。
- **启发式永远不是正确性边界**：L2 "看起来像登录态"（v2 阶段的软启发式）即便留到 v3 也不会破坏正确性——因为 L3 活体探针是唯一权威裁判。凡是在代码里写启发式，都要问一句"如果它判错会有什么后果"；后果不可承受就不是启发式，是 business rule，不能用 fuzzy 逻辑做。
- **依赖轻量化也是 UX**：v3 撤掉 `[browser]` extra 后，`uv sync` 再也不拖 `pycryptodomex` / `lz4` / `pywin32` 这 7 个二进制包——一个新用户跑 `uv sync` 从 ~30 秒降到 ~5 秒，依赖失败面也小了一个数量级。"装起来很快"本身就是产品体验的一部分，不是事后才想到的优化。
