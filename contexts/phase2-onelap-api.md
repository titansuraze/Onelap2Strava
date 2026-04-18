# Phase 2：顽鹿接口侦察与接入记录

> 本文档是顽鹿 Web 端的接口清单 + 侦察指引，是接入层 `src/onelap2strava/onelap/` 的**单一事实源**。顽鹿私有接口会改版，每次发现异动请回到这里校准。

## 1. 目前已知的接口（基于公开参考项目和实测）

> 来源：开源项目 [`moruoxian/SyncOnelapToXoss`](https://github.com/moruoxian/SyncOnelapToXoss) 的 `fetch_activities` / `download_fit_file` + 多个 CSDN 博客交叉印证。存量代码已按这三个接口跑通过一轮（参考项目广泛使用）。

### 1.1 认证态：Cookie 为主

顽鹿 Web 端走传统 Cookie/Session 体系，没有明显的 `Bearer token`。**有效登录后，浏览器里打到 `u.onelap.cn` 的请求，`Cookie` 头里的内容就是凭证**。Cookie 通常种在父域 `.onelap.cn`，对 `www` 和 `u` 子域同时有效。

**实测观察到的业务 Cookie 键**（来自实际登录后 DevTools 查看）：

- `OTOKEN` — 主要鉴权 token，JWT-like 结构（`{iv, value, mac}` 的 Base64 JSON）。
- `XSRF-TOKEN` — CSRF 防御 token，同样的 `{iv, value, mac}` 结构。
- `ouid` — 用户 ID（明文整数）。
- `_c_WBKFRo` / `_nb_ioWEgULi` — 埋点类 Cookie，可能不参与鉴权但建议一并透传。

> 注意：多个 CSDN 博客提到的 `PHPSESSID` / `access_token` 在实测里**并不存在**——说明顽鹿后端已经更换鉴权实现（或者博客作者写的本来就不准）。这正是"**全量透传**"策略的意义：把浏览器 Cookie 串整条塞进请求头，不挑字段，以规避键名变动和二手信息误差。

#### Cookie 导入：只保留手粘一条路径

CLI 只提供一个入口：

```bash
onelap-login               # 交互式 prompt（hide_input=True）
onelap-login --cookie "…"  # 非交互，适合脚本化场景
```

产物是 `data/.onelap_cookies.json`。完成后 CLI 会做一次 **live probe**（`OnelapClient.list_activities(limit=1)`）——服务端返回 JSON（见 `_raise_if_auth_required`）才算通过；返回登录页 HTML 就立刻报错要求重登，不让坏 Cookie 留到 `sync` 时才暴露。

**曾经实现过自动读浏览器 Cookie（`--from-browser` + `browser-cookie3`）**，后来完全下线。过程和理由见 [`phase2-onelap-scraping.md`](phase2-onelap-scraping.md) 的 §2.7；一句话总结：Chrome / Edge 125+ 的 ABE 在 Windows 上即便以管理员身份也经常解不出 Cookie 密钥，加上不同机器 `browser-cookie3` 版本导致的错误措辞差异，维护"时灵时不灵"的自动路径对用户体验是净负收益。

### 1.2 活动列表

```
GET http://u.onelap.cn/analysis/list
Headers:
  Cookie: <从浏览器拷的完整 cookie 串>
  User-Agent: <任意常见 UA>
```

响应（JSON 片段，关键字段）：

```json
{
  "data": [
    {
      "created_at": 1712345678,         // unix 秒，活动开始时间
      "totalDistance": 32170,           // 米
      "elevation": 123,                 // 米
      "durl": "/analysis/download/XXXXXX.fit",  // Fit 下载路径（相对）
      "fileKey": "MAGENE_C506_XXXX.fit",        // 原始文件名
      "fitUrl": "..."                           // 可能的备用文件名
    },
    ...
  ]
}
```

**返回顺序**：参考项目从不排序直接用，结合 `created_at` 字段看，默认是**最新在前**（最常见的约定）。接入层统一按 `created_at` 降序排一次兜底。

### 1.3 Fit 下载

```
GET http://u.onelap.cn{activity.durl}
Headers:
  Cookie: <同上>
```

返回：`application/octet-stream`，Body 是 Fit 二进制。可从 `Content-Disposition: filename="..."` 拿文件名；拿不到就用 `fileKey` / `fitUrl` 兜底，最后兜底 `activity.fit`。

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
| 端点配置 | `src/onelap2strava/onelap/client.py`（模块常量） | `BASE_URL_U`、`PATH_LIST`、`USER_AGENT` |
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
