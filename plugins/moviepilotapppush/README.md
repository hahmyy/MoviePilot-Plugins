# MoviePilotAppPush（MoviePilot V2 版）

把 MoviePilot 服务端通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。
功能与 V3 版（plugins.v3/moviepilotapppush）一致，按 V2 宿主 SDK 实现。

## 约定

- 插件 ID：`MoviePilotAppPush`（App 端据此渲染专用配置页并调用 `/run` 测试接口）。
- 配置项：
  - `enabled`：是否启用
  - `apikey`：Push Key（App 调测试接口时的凭据）
  - `token`：App Push Token（即极光 Alias，推送目标）
  - `appkey`：极光 AppKey（服务端鉴权用户名）
  - `mastersecret`：极光 Master Secret（服务端鉴权密码，仅服务端保存）

## 接口

- `GET /api/v1/plugin/MoviePilotAppPush/run?apikey=<PushKey>`
  - 需携带登录态（`auth: "bear"`），校验 apikey 后向配置的 token 发送一条测试通知。
  - 响应：`{"code": 0, "msg": "..."}`；失败 `code` 非 0。

## 事件

- 监听 `EventType.NoticeMessage`（`notice.message`），把服务端通知转发为极光推送。
- 事件数据取 `title` / `text`，并把 `channel` / `type` / `source` / `userid`
  放入通知 `extras`，供 App 后续做点击跳转扩展。
- 可按需扩展监听 `TransferComplete` / `DownloadAdded` / `SubscribeAdded` 等事件。

## 实现说明（已按 V2 宿主 SDK 核对）

- 事件与日志：`app.core.event` 的 eventmanager/Event、`app.log` 的 logger；
  V2 eventmanager 把同步 handler 放线程池执行，因此事件方法为同步实现。
- HTTP：`app.utils.http.RequestUtils`（requests 同步）。
  `post(url, data=None, json=None, **kwargs)` 返回 requests.Response；
  网络异常默认被吞掉返回 None，HTTP 4xx/5xx 通过响应状态码判定。
- JPush v3：`POST https://api.jpush.cn/v3/push`，Basic Auth
  `appkey:mastersecret`；目标 `audience.alias`；平台关键字使用极光官方
  的 `android` / `ios` / `hmos`（鸿蒙），通知体按平台细分，hmos 带
  `category: "IM"`；成功响应含 `msg_id`，失败为 `error.code` /
  `error.message`。

## 详情页与版本

- 插件详情页展示“最近一次测试结果”（测试时间、目标 Alias 脱敏、返回信息）。
- 插件页（详情页）与首页仪表盘都展示调用次数、成功/失败次数、连接状态与最近 20 条历史消息；详情页同时保留最近一次测试结果。
- 支持消息类型多选筛选（媒体服务器/订阅/整理入库/资源下载/站点/手动处理/其它/智能体/插件），不选则转发全部。
- 配置页可自定义测试标题与内容，并用“发送测试（保存后立即发送一条）”开关立即发送；测试接口支持 title/text 查询参数覆盖。
- 图标：仓库根 `icons/MoviePilotAppPush.png`（V2/V3 共用），索引 `icon` 字段一致。
- 版本：`package.json` / `package.v2.json` 内 `MoviePilotAppPush` 版本 0.1.9，与插件类 `plugin_version` 一致。

## 华为 Push Kit 直连渠道（可选）

- `channel` 选 `huawei` 时，`token` 字段填华为 Push Token（不再是极光 Alias）。
- 需配置 `project_id`（AGC 项目 ID）和 `service_account_json`（AGC 服务账号 JSON，含 key_id /
  sub_account / private_key）；`appid` 为 Client ID，v3 接口不直接使用，仅作备用记录。
- 服务端按华为官方文档生成 PS256 服务账号 JWT，优先换取 access_token（
  grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer），换取不可用时回退官方支持的
  JWT 直连方式；调用 `POST /v3/{projectId}/messages:send`，Header `push-type: 0`，
  请求体为 `payload.notification{category,title,body}` + `target.token` + `pushOptions`。
- 安全：服务账号 JSON 只存服务端插件数据，配置页不回显；日志、页面、仪表盘与 /run
  响应都不会出现 private_key。

## 配置页排版与 JSON 上传

- 推送渠道切换后只显示当前渠道需要的输入框：极光字段用 `v-show` 控制为 `channel !== 'huawei'`，
  华为字段控制为 `channel === 'huawei'`，避免所有渠道字段挤在同一版面。
- 华为服务账号支持两种输入方式：直接粘贴 JSON 到文本框，或用文件选择框上传 `.json`；
  上传由前端读取文件内容写入 JSON 文本字段，服务端保存时再解析识别，表单 model 不保存 File 对象。
