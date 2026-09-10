# AppPushMsg

把 MoviePilot 服务端通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。

## 约定

- 插件 ID：`AppPushMsg`（App 端据此渲染专用配置页并调用 `/run` 测试接口）。
- 配置项：
  - `enabled`：是否启用
  - `apikey`：Push Key（App 调测试接口时的凭据）
  - `token`：App Push Token（即极光 Alias，推送目标）
  - `appkey`：极光 AppKey（服务端鉴权用户名）
  - `mastersecret`：极光 Master Secret（服务端鉴权密码，仅服务端保存）

## 接口

- `GET /api/v1/plugin/AppPushMsg/run?apikey=<PushKey>`
  - 需携带登录态（`auth: "bear"`），校验 apikey 后向配置的 token 发送一条测试通知。
  - 响应：`{"code": 0, "msg": "..."}`；失败 `code` 非 0。

## 事件

- 监听 `EventType.NoticeMessage`（`notice.message`），把服务端通知转发为极光推送。
- 事件数据取 `title` / `text`，并把 `channel` / `type` / `source` / `userid`
  放入通知 `extras`，供 App 后续做点击跳转扩展（见对接文档第 9 节预留点）。
- 可按需扩展监听 `TransferComplete` / `DownloadAdded` / `SubscribeAdded` 等事件。

## 推送实现说明（已按真实 V3 宿主核对）

- HTTP 使用宿主 `app.sdk.network.AsyncRequestUtils`：
  `post(url, data=None, json=None, **kwargs)`，`headers` 走 kwargs；
  `raise_exception=True` 时网络异常抛出（httpx2.RequestError 家族），
  HTTP 4xx/5xx 不会抛出，通过响应 `status_code` 判定。
- 事件处理器使用 `async def`，宿主 V3 `eventmanager` 会把异步 handler
  提交到主事件循环并 `await`（见宿主 `app/runtime/event/dispatch.py`
  `dispatch_broadcast`），因此无需自行 `asyncio.create_task`。
- JPush v3：`POST https://api.jpush.cn/v3/push`，Basic Auth
  `appkey:mastersecret`；目标 `audience.alias`；平台关键字使用极光官方
  的 `android` / `ios` / `hmos`（鸿蒙），通知体按平台细分，hmos 带
  `category: "IM"`；成功响应含 `msg_id`，失败为 `error.code` /
  `error.message`。

## 资源与版本

- 插件详情页展示“最近一次测试结果”（测试时间、目标 Alias 脱敏、返回信息）。
- 插件仪表盘展示调用次数、成功/失败次数、连接状态与最近 20 条历史消息（已声明仪表盘元信息）。
- 支持消息类型多选筛选（媒体服务器/订阅/整理入库/资源下载/站点/手动处理/其它/智能体/插件），不选则转发全部。
- 图标：`icons/AppPushMsg.png`，与 `package.v3.json` 的 `icon` 保持一致。
- 版本：类 `plugin_version`、`package.v3.json.version` 与 `history` 顶部版本一致。