# MoviePilotAppPush（MoviePilot V3 版）

把 MoviePilot 的服务端通知通过华为官方推送服务（HarmonyOS Push Kit）送达鸿蒙客户端（MP tool），
应用退到后台或已被划掉也能收到。

功能与 V2 版（`plugins.v2/moviepilotapppush`）一致，按 V3 宿主 SDK 实现（异步事件与异步 HTTP）。

## 约定

- 插件 ID：`MoviePilotAppPush`，显示名「MP tool推送」（App 端据此渲染专用配置页并调用 `/run` 测试接口）。
- **只有华为官方推送一条链路**：插件 → 内置中转（地址内置在插件代码中，不对外展示）→ 华为 Push Kit。
  早期的极光 JPush 与「服务账号直连华为」两条渠道已移除，不留开关，也不再读取相关凭据。
- 配置页只有四项：`enabled`（启用）、`msgtypes`（推送哪些通知）、`testtitle` / `testtext`（测试内容）。
  Push Key 已去掉：它从来没参与过推送，测试接口本身要求登录态。
- 兼容旧配置：`channel` / `token` / `appkey` / `mastersecret` / `appid` / `project_id` /
  `service_account_json` / `huawei_category` 可能还留在存储里，但**一律不读、不用**；
  历史配置里写死的 `channel` 也会被忽略，推送固定走内置中转。

## 接口

- `GET /api/v1/plugin/MoviePilotAppPush/run?title=&text=`
  - 需携带登录态（`auth: "bear"`），向已绑定设备（无绑定时回退 `token`）发一条测试通知。
  - `title` / `text` 可覆盖配置里的测试标题与内容。
  - 响应：`{"code": 0, "msg": "..."}`；失败 `code` 非 0。
  - 旧调用方仍可带 `apikey` 参数，为了兼容保留，不再参与校验。
- `POST /api/v1/plugin/MoviePilotAppPush/device/bind`（`auth: "bear"`）
  - App 开启通知后登记设备：`device_id` / `push_token` / `device_name` / `notify_enabled`。
  - 用户身份取自宿主已验签的 Bearer 令牌，不读取请求体里的用户字段。
- `POST /api/v1/plugin/MoviePilotAppPush/device/notify`：开关单台设备的通知。
- `POST /api/v1/plugin/MoviePilotAppPush/device/unbind`：解除绑定。
- `GET /api/v1/plugin/MoviePilotAppPush/device/remove?device_id=...`：插件详情页删除按钮调用。
- `GET /api/v1/plugin/MoviePilotAppPush/device/list`：已绑定设备列表（Push Token 脱敏）。

## 设备绑定

- 首次启用时生成安装标识 `install_id`（如 `mp_1a2b3c4d`），配置页与详情页都会展示，用于区分站点。
- App 在「设置 → 通知」开启后调用 `device/bind`，按 `device_id` 登记设备；同一设备重复绑定
  只刷新 Push Token 与设备名，不会重复占位（上限 20 台）。
- 推送逐个投递到「已绑定且通知开启」的设备；没有绑定设备时回退到配置里的 `token`，老配置不受影响。
- 设备被判「已失效」（App 卸载或 Token 不匹配）后不再投递，详情页可逐台删除。
- 详情页展示设备名、用户、状态与最近推送时间。

## 事件

- 监听 `EventType.NoticeMessage`（`notice.message`），把服务端通知转发为华为推送。
- 事件数据取 `title` / `text`，并把 `channel` / `type` / `source` / `userid`
  放入通知点击数据，供 App 做点击跳转（点击后落到「消息中心」）。
- 可按需扩展监听 `TransferComplete` / `DownloadAdded` / `SubscribeAdded` 等事件。

## 实现说明（已按真实 V3 宿主核对）

- HTTP 使用宿主 `app.sdk.network.AsyncRequestUtils`：
  `post(url, data=None, json=None, **kwargs)`，`headers` 走 kwargs；
  `raise_exception=True` 时网络异常抛出（httpx2.RequestError 家族），
  HTTP 4xx/5xx 不会抛出，通过响应 `status_code` 判定。
- 事件处理器使用 `async def`，宿主 V3 `eventmanager` 会把异步 handler
  提交到主事件循环并 `await`（见宿主 `app/runtime/event/dispatch.py`
  `dispatch_broadcast`），因此无需自行 `asyncio.create_task`。
- 推送体由中转侧拼装：插件只提交 `title` / `body` / `click` 与设备目标，
  华为 v3 `messages:send` 的 JWT 签名、projectId 都在中转上，插件不接触服务账号。

## 资源与版本

- 插件详情页：设备卡片、最近一次测试结果、一键「发送测试推送」按钮（直接调 `/run`，无需先保存）。
- 插件页与首页仪表盘都展示调用次数、成功/失败次数、连接状态与最近 20 条历史消息；
  历史消息里的渠道名统一显示为「官方推送」。
- 支持消息类型多选筛选（媒体服务器/订阅/整理入库/资源下载/站点/手动处理/其它/智能体/插件），不选则转发全部。
- 图标：`icons/MoviePilotAppPush.png`，与 `package.v3.json` 的 `icon` 保持一致。
- 版本：0.1.x 为 V1/V2 索引，V3 从 1.0.x 起；当前 `plugin_version` 与 `package.v3.json` 均为 1.0.22。

## 推送链路（内置中转）

- 推送统一 `POST <内置中转地址>/v1/messages/`（结尾斜杠不能省），
  Header `Authorization: Bearer <install_secret>`、`X-Install-Id: <install_id>`。
- 请求体：`install_id` / `device_id` / `target{type,value}` /
  `notification{category,title,body,click}` / `client_msg_id`。
- 中转负责鉴权、限流、失败重试与华为回执对账；插件侧只记录本地最近一次结果与历史消息。
- 中转地址不写在文档与界面里；需要更换时属于网关侧运维动作。

## 推送 extras 字段契约（点击通知展示详情）

所有字段都放在华为 `payload.notification.clickAction.data` 里，App 点击后按这套字段解析：

| 字段 | 说明 |
|---|---|
| page | 固定 `system-message`，App 点击后跳转目标 |
| title | 通知标题（截断 120 字） |
| text | 通知正文（截断 300 字，避免华为 4096 字节消息体限制） |
| msgtype | NotificationType 名称（如 Subscribe、MediaServer） |
| channel | 来源渠道（如 wechat、telegram） |
| source | 消息来源 |
| userid | 目标用户 ID |
| ts | 发送时间戳（秒） |
| type | 兼容旧版别名，值与 msgtype 一致 |

字段名与含义不允许随意变更：App 端已按这张表解析通知详情与点击落地页。

## 已移除的历史渠道

- 早期版本支持 `channel=jpush`（极光）与 `channel=huawei`（服务账号直连华为），两套代码路径
  仍留在源码里但已不可达：初始化时 `channel` 固定为内置中转。
- 老站点升级后无需改配置，也不会再因为本地服务账号过期、算法不匹配而报 `80200001` 之类的认证错误。
- 配置里残留的 `token` / `appkey` / `mastersecret` / `project_id` / `service_account_json`
  只是留在存储里，保存一次配置就会被清掉。
