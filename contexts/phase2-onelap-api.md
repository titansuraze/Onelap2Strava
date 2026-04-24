# Phase 2：顽鹿接口侦察与接入记录

> 本文档是顽鹿 Web 端的接口清单 + 侦察指引，是接入层 `src/onelap2strava/onelap/` 的**单一事实源**。顽鹿私有接口会改版，每次发现异动请回到这里校准。

## 1. 目前已知的接口（基于公开参考项目和实测）

> 来源：开源项目 [`moruoxian/SyncOnelapToXoss`](https://github.com/moruoxian/SyncOnelapToXoss) 的 `fetch_activities` / `download_fit_file` + 多个 CSDN 博客交叉印证。存量代码已按这三个接口跑通过一轮（参考项目广泛使用）。

### 1.1 认证态：Cookie +（OTM 上常见的）`Authorization: Bearer`

顽鹿 Web 端以 **Cookie/Session** 为站点登录基础；`https://u.onelap.cn/record` 的 **OTM** JSON 接口在实测中往往还带 **`Authorization: Bearer <JWT>`**（与 Cookie 同见于 Network 的 XHR/Fetch 请求行），列表与 FIT 下载以 **「Cookie + 该 JWT」** 同一会话**透传**最稳。Cookie 可只含少量键（如 `ouid`、`onelap_web_session`、分析类 `_*` 等），**不必然**再出现老文档里写的 `OTOKEN`——以 `onelap-login` 的 **[ok] 验证**为准。

**历史上仍常见的 Cookie 键**（按环境有无差异很大，以浏览器 Application / 抓包为准）：

- `OTOKEN` / `XSRF-TOKEN` 等：部分页面或老路径仍有；**OTM 上未必出现**。
- `ouid` — 用户 ID（明文整数）。
- `onelap_web_session` — 站点会话；`_c_*` 等 — 常一并透传。

> 多份二手资料提到 `PHPSESSID` 等，与当前 u 子域不一定一致。策略不变：**从「Response 为 JSON 的 u.onelap.cn XHR」复制整行 `Cookie:`，并复制同一会话的 `Bearer` JWT** 写入 `data/.onelap_cookies.json`（见 `onelap-login`）。

#### Cookie 导入：只保留手粘一条路径

CLI 只提供一个入口：

```bash
onelap-login               # 交互式 prompt（hide_input=True）
onelap-login --cookie "…"  # 非交互，适合脚本化场景
```

产物是 `data/.onelap_cookies.json`。完成后 CLI 会做一次 **live probe**（`OnelapClient.list_activities(limit=1)`）——服务端返回 JSON（见 `_raise_if_auth_required`）才算通过；返回登录页 HTML 就立刻报错要求重登，不让坏 Cookie 留到 `sync` 时才暴露。

**曾经实现过自动读浏览器 Cookie（`--from-browser` + `browser-cookie3`）**，后来完全下线。过程和理由见 [`phase2-onelap-scraping.md`](phase2-onelap-scraping.md) 的 §2.7；一句话总结：Chrome / Edge 125+ 的 ABE 在 Windows 上即便以管理员身份也经常解不出 Cookie 密钥，加上不同机器 `browser-cookie3` 版本导致的错误措辞差异，维护"时灵时不灵"的自动路径对用户体验是净负收益。

### 1.2 活动列表

**历史**：`GET http://u.onelap.cn/analysis/list` 曾直接返回与下述同构的 JSON；约 2026 起该地址常变 HTML/重定向，**不要再依赖**作入口。

**当前**：在 [`/record`](https://u.onelap.cn/record) 前端下，列表由 OTM 接口提供。其中 **`/api/otm/ride_record/list` 在浏览器里为 `POST` + `Content-Type: application/json`**（带分页类请求体）；本仓库对该 URL 会依次用若干常见 JSON 体发 POST，再回退到 GET。其它候选与旧 `/analysis/list` 仍以 GET 为主。可用环境变量 **`ONELAP_LIST_URL`** 覆盖为抓包得到的一条完整 URL。

```
POST https://u.onelap.cn/api/otm/ride_record/list
  Body: 与抓包一致为 ``{"page":1,"limit":20}``；程序优先尝试 ``page`` + 较大 ``limit``，并保留其它分页键名作备选
GET  https://u.onelap.cn/api/otm/ride_record/record_list
GET  https://u.onelap.cn/api/otm/ride_record/records
...（及候选末端的旧 /analysis/list）
Headers:
  Cookie: <完整 cookie 串>
  Referer: https://u.onelap.cn/record/
  Authorization: Bearer <JWT>   # 与 Cookie 同见于浏览器，sync 时建议一并配置（``onelap-login --bearer`` / ``.onelap_cookies.json`` 的 ``bearer``）
  User-Agent: <常见桌面 UA>
```

响应常见 **`{ "code": 200, "data": [ {...}, ... ] }`**，亦兼容无 `code` 的老形 **`{ "data": [ ... ] }`**；`data` 为对象时或键为 `list` / `records` 等，见 :mod:`onelap2strava.onelap.client` 的解析。

约 2026-04 起，列表 `POST /ride_record/list` 的 Payload 抓包为 `{"page":1,"limit":20}`；本仓库会优先用 `page` + 较大 `limit` 等候选体。

**列表项可能是「摘要」行**：仅含 `id`、**`distance_km`**、**`start_riding_time`** 等，**无** `durl` / `fileKey`。接入层在 :mod:`onelap2strava.onelap.models` 中将其转为 ``Activity``（占位 `download_path`、时间用 `start_riding_time` 等、`raw` 里补 `_id` 供 Referer），下载前再由 :mod:`onelap2strava.onelap.client` 尝试 **`/api/otm/ride_record/detail`**（POST/GET 若干形式）**合并** `fileKey` / `durl`；仍无法下载时再对照浏览器详情页 Network 中真实 detail URL 校准。老列表仍可能直接给 `durl` + `fileKey`。

**返回顺序**：接入层将解析结果统一按时间（含摘要里的东八区时间）转 UTC 后**降序**排一次。

### 1.3 Fit 下载

```
GET http://u.onelap.cn{activity.durl}
Headers:
  Cookie: <同上>
```

返回：`application/octet-stream`，Body 是 Fit 二进制。可从 `Content-Disposition: filename="..."` 拿文件名；拿不到就用 `fileKey` / `fitUrl` 兜底，最后兜底 `activity.fit`。

**补充（约 2026-04）**：运动记录前端迁到 `https://u.onelap.cn/record`，详情页「下载」走 OTM，与 `durl` 里 `fits.rfsvr.net` 直链并存；新链形式为：

```
GET https://u.onelap.cn/api/otm/ride_record/analysis/fit_content/{BASE64(fileKey路径)}
```

其中 `fileKey` 为 **UTF-8 路径**字符串，例如 `geo/20260424/MAGENE_....fit`（**来源**可是列表、或列表无 `fileKey` 时由 **detail 接口** 补全），经标准 Base64 编码后作为路径**最后一段**（可含 `=` 填充）。需 **Cookie**；OTM 上通常还带 **`Authorization: Bearer <JWT>`** 与 **`Referer: https://u.onelap.cn/record/details?id=<_id>`**（`raw` 里用 **`_id` 或 `id`** 均可）。**勿**在浏览器地址栏直接打开 `fit_content` URL 来验证——整页导航不会带 `Authorization`，易见 `403`「Authorization fail!」。接入层在 :mod:`onelap2strava.onelap.client` 中对该 GET 带上述 Referer，JWT 在 ``data/.onelap_cookies.json`` 的 ``bearer`` 字段（见 ``onelap-login --bearer``），下载候选**优先**该端点，再回退 `durl` / 旧式 ``/analysis/download/...``。

### 1.4 登录（待确认）

两条可能路径：

- **直接 API**（多个 CSDN 博客提到，但没有一手确认）：
  ```
  POST https://www.onelap.cn/api/login
  Body: username=<手机号>&password=<MD5(原密码)>
  ```
  待抓包验证请求体结构（form-urlencoded 还是 JSON？是否需要 CSRF token？）。

- **浏览器自动化**（参考项目实际使用）：把登录页 `https://www.onelap.cn/login.html` 交给 DrissionPage/Selenium 驱动，表单提交后抓取 Cookie。

**当前工具的选择**：两者都不做，改为**手动 Cookie 模式**——用户从浏览器复制 Cookie 串，`onelap-login` 命令接收并持久化。理由：

1. 最稳，完全不依赖顽鹿的登录实现细节。
2. 顽鹿 Cookie 过期周期较长（实测常见以天/周计），不需要频繁重登。
3. 后续如果抓包确认了 API 登录结构，接入层加一个 `login(phone, password)` 方法即可，不改其他代码。

## 2. 侦察指引：未来需要确认或新接口时怎么抓

### 2.1 工具选择（Windows）

推荐 **Chrome DevTools + 浏览器直接操作**（最简单，够用）：

**提取 Cookie / 观察列表接口的标准流程**（最小化踩坑）：

1. 用 Chrome 登录 https://www.onelap.cn。
2. 地址栏**直接访问** http://u.onelap.cn/analysis/list 回车。这是纯 JSON 接口，成功的话页面直接显示一段 JSON 文本。
3. 按 `F12` 打开 DevTools → **Network** 标签 → 按 `F5` 刷新。
4. Network 列表里找名字叫 **`list`** 的那条 document 请求（永远在最上面，因为当前页面 URL 就是它）。
5. 点它 → **Headers** 看 Request/Response Headers、**Preview/Response** 看 JSON body。复制 Cookie 串就取 Request Headers 里 `Cookie:` 那一行。
6. 需要 cURL 复现时：右键 → `Copy` → `Copy as cURL (bash)`，WSL/Git Bash 里可以直接跑。

> **为什么不是 `/analysis`？** 那是一个富页面，会连带加载几十个百度埋点、CDN 静态资源等请求，Network 列表混杂严重，复制 Cookie 时很容易选到第三方请求（实测会导致粘贴的 Cookie 里全是 `BAIDUID_BFESS` 等百度域 Cookie，完全无法登录）。`/analysis/list` 是纯 JSON 端点，Network 里就一条 `list` document 请求，不可能错。

**观察其他接口**（翻页、下载等）：在这个标准流程基础上勾 `Preserve log`，然后去 `/analysis` 页面做对应操作，接口请求就会出现在 `list` 下面——但过滤关键字应该用 **`u.onelap.cn`** 作为域名精确匹配，不要用泛关键字 `onelap`。

如果需要抓手机 App 或小程序流量，那才上 **mitmproxy**（Windows 下 `pip install mitmproxy`，`mitmweb` 起 Web UI，手机 Wi-Fi 挂代理 + 装根证书）。**本项目首选 Web 端**，因为已经够用且免去证书烦恼。

### 2.2 侦察登录接口（如果要）

1. DevTools Network 面板打开，清空。
2. 访问 `https://www.onelap.cn/login.html`，填手机号和密码，提交。
3. Network 里找第一个 `POST` 类型请求，记录：
   - URL
   - Request Headers（`Content-Type`、有没有 `X-Csrf-Token` 之类）
   - Request Body（看是 form 还是 JSON，密码字段是原文还是 hash）
   - Response Body（有没有 token、过期时间）
   - Response 里 `Set-Cookie` 了哪些 Cookie
4. 用 `Copy as cURL` 在命令行重放一次，若能稳定拿到 200 + Set-Cookie，就是可编码的。

### 2.3 接口改版时的校准流程

每次发现 `sync` 跑挂，按顺序排查：

1. Cookie 是否过期？浏览器重登 `/analysis/list`，按 §2.1 的流程重粘 Cookie，重跑 `onelap-login`。
2. `GET /analysis/list` 响应结构是否变化？对比"已知字段表"与最新响应。
3. `durl` 还是相对路径吗？是否改成了签名 URL？
4. 更新本文档 §1 → 同步改 `src/onelap2strava/onelap/` 里相应的字段映射。

## 3. 接入层对接契约

代码按以下假设编写，接口层改版时只需要调整这几个点：

| 契约 | 位置 | 内容 |
| --- | --- | --- |
| 端点配置 | `src/onelap2strava/onelap/client.py`（模块常量） | `BASE_URL_U`、`PATH_LIST`、`PATH_OTM_FIT_CONTENT`、`USER_AGENT` |
| Activity 字段映射 | `src/onelap2strava/onelap/models.py::Activity.from_api` | `created_at` / `durl` / `fileKey` / `totalDistance` 等 |
| 认证载体 | `src/onelap2strava/onelap/auth.py` | Cookie 持久化在 `data/.onelap_cookies.json` |
| 登录入口 | CLI 的 `onelap-login` 子命令 | 目前只有手粘一条路径；未来加 API 登录时在这里扩展第二条 |

## 4. 已知限制与风险

- **只在中国大陆网络可用**：顽鹿服务对海外 IP 有封锁。
- **Cookie 过期表现**：实测过期时 `/analysis/list` 会返回 `200` + HTML（登录页），**不是** 401。接入层要把"返回不是 JSON"识别为"需要重登"，并给出清晰提示。
- **Chrome 125+ ABE 加密让"自动读 Cookie"不可靠**：2024 中 Chrome / Edge 引入 App-Bound Encryption 后，跨进程读取 Cookie 的门槛从"有 DPAPI 凭据即可"抬到"需要 SYSTEM/admin 才能 unsealed"。加上不同 `browser-cookie3` 版本抛出的错误措辞不同（`This operation requires admin` / `Unable to get key for cookie decryption` 都观测到过），同一台机器上也会随升级突然失效。**因此 Phase 2 已经彻底下线 `--from-browser` 路径**，只保留手粘——完整过程见 [`phase2-onelap-scraping.md`](phase2-onelap-scraping.md) §2.7。
- **顽鹿随时可能改接口**：这就是为什么接入层和业务层严格分离。
- **只供自己账号使用**：顽鹿没有开放 API，本项目不是多账号池产品。

## 5. 下次抓包要补的信息（TODO）

- [ ] 确认 `/api/login` 是否真实可用，如果可用记录完整请求/响应（Phase 3 可选）。
- [ ] 确认 `/analysis/list` 是否支持分页参数（`page` / `limit` / `offset`）以便未来做历史全量同步。
- [x] ~~观察 Cookie 过期的具体 HTTP 行为~~ → 已确认：`200 + Content-Type: text/html`（登录页），接入层已处理，见 `client.py::_raise_if_auth_required`。
- [ ] 实测记录 Cookie 生命周期：粘贴一次后累计可用多少天，是否有规律（固定窗口 / 滑动窗口 / 服务端主动踢）。需要长期运行一段时间才能沉淀。
