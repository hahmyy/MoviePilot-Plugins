# MoviePilotAppPush（MoviePilot V2 版）

把 MoviePilot 服务端通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。
功能与 V3 版（plugins.v3/moviepilotapppush）一致，按 V2 宿主 SDK 实现。

## 约定

- 插件 ID：`MoviePilotAppPush`（App 端据此渲染专用配置页并调用 `/run` 测试接口）。
- 推送链路：插件 → 内置中转（地址内置在插件代码中，不对外展示）→ 华为 Push Kit；用户无需配置渠道或服务端凭据。
- 配置页只保留测试项：`enabled`、`apikey`（测试接口凭据）、`msgtypes`、`testtitle`、`testtext`。
- 兼容旧配置：`channel` / `token` / `appkey` / `mastersecret` / `appid` / `project_id` /
  `service_account_json` / `huawei_category` 仍保留在存储中，但不再在设置页展示。

## 接口

- `GET /api/v1/plugin/MoviePilotAppPush/run?apikey=<PushKey>`
  - 需携带登录态（`auth: "bear"`），校验 apikey 后向已绑定设备（无绑定时回退 `token`）发送一条测试通知。
  - 响应：`{"code": 0, "msg": "..."}`；失败 `code` 非 0。
- `POST /api/v1/plugin/MoviePilotAppPush/device/bind`（`auth: "bear"`）
  - App 开启通知后登记设备：`device_id` / `push_token` / `device_name` / `notify_enabled`。
  - 用户身份取自宿主已验签的 Bearer 令牌，不读取请求体里的用户字段。
- `POST /api/v1/plugin/MoviePilotAppPush/device/notify`：开关单台设备的通知。
- `POST /api/v1/plugin/MoviePilotAppPush/device/unbind`：解除绑定。
- `GET /api/v1/plugin/MoviePilotAppPush/device/remove?device_id=...`：插件详情页删除按钮调用。
- `GET /api/v1/plugin/MoviePilotAppPush/device/list`：已绑定设备列表（Push Token 脱敏）。

## 设备绑定

- 首次启用时生成安装标识 `install_id`（如 `mp_1a2b3c4d`），页面展示，用于区分站点。
- App 在「设置 → 通知」开启后调用 `device/bind`，按 `device_id` 登记设备；同一设备重复绑定
  只刷新 Push Token 与设备名，不会重复占位（上限 20 台）。
- 推送逐个投递到“已绑定且通知开启”的设备；没有绑定设备时回退到配置里的 `token`，
  老配置不受影响。
- 华为返回 `80300007`（Token 全部无效）或连续失败 5 次时，该设备标记为「已失效」，不再投递。
- 详情页展示设备名、用户、状态与最近推送时间，可逐台删除。

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
- 版本：`package.json` / `package.v2.json` 内 `MoviePilotAppPush` 版本 0.1.19，与插件类 `plugin_version` 一致。

## 内置中转（当前默认）

- `channel` 默认 `gateway`：推送统一 `POST <内置中转地址>/v1/messages/`（结尾斜杠不能省），
  Header `Authorization: Bearer <install_secret>`、`X-Install-Id: <install_id>`。
- 请求体：`install_id` / `device_id` / `target{type,value}` /
  `notification{category,title,body,click}` / `client_msg_id`；中转负责换取华为 access_token
  并调用 v3 接口。
- 设置页不再展示渠道与凭据字段；`install_id` 在配置页与详情页可见，设备由 App 绑定。

## 旧直连渠道（仅兼容旧配置）

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

## 推送 extras 字段契约（点击通知展示详情）

极光与华为两条渠道下发同一组字段，字段名与含义不允许随意变更：

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

极光放在 notification.extras 与各平台 notification.<平台>.extras；华为放在
payload.notification.clickAction.data。App 按以上字段解析为通知详情。

## 插件详情页（测试与统计）

- 详情页顶部提供“发送测试推送”按钮：点击后直接调用固定接口
  `/api/v1/plugin/MoviePilotAppPush/run`（前端相对路径 `/plugin/MoviePilotAppPush/run`），
  使用配置页已保存的测试标题/内容，无需先保存再测试；完成后页面会自动刷新统计。
- 详情页与首页仪表盘都包含成功率/失败率环形仪表（VProgressCircular），
  以成功次数与失败次数计算百分比；没有推送记录时显示 0% 与“暂无推送记录”。
- 测试开关不再放在配置表单里（`onlyonce` 字段保留兼容，默认关闭）。
