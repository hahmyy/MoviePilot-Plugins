# 任务书（插件侧）：为 AppPushMsg 增加「华为 Push Kit 直连」推送渠道

> 仓库：MoviePilot-Plugins（hahmyy fork）
> 目标文档：本仓库 docs/ 与 App 仓库 docs/harmonyos-push-integration.md 第 13 节
> 前置：先读 AGENTS.md 与 docs/Plugin_Development.md，遵守 V2/V3 双布局与版本门禁约定

## 1. 背景与目标

现有 AppPushMsg 插件（V3 版本 1.0.5 / V2 版本 0.1.5）只实现了极光推送（方案 A）：
服务端用 Basic Auth（appkey:mastersecret）调极光 v3 接口，推送目标为极光 Alias。

本任务是在**不新增插件、不改插件 ID、不改 /run 契约**的前提下，为同一插件增加
「华为 Push Kit 直连」渠道（方案 B）：服务端直接用华为账号的服务账号凭据换取令牌，
调用华为推送 REST 接口把通知送达鸿蒙设备，不再经过极光。

必须保持不变的兼容点（App 端依赖）：

- 插件 ID 仍为 `AppPushMsg`（App 端按此 ID 匹配专用配置页）。
- 测试接口仍为 `GET /api/v1/plugin/AppPushMsg/run?apikey=...`，响应仍为 `{code, msg}`，
  且成功时 `code` 为 0。
- App 端配置页仍只需要读写 `enabled` / `apikey` / `token` 三个字段。

## 2. 需要新增的配置项

在现有配置（enabled / apikey / token / appkey / mastersecret）基础上增加：

| 字段 | 说明 |
|---|---|
| channel | 推送渠道：`jpush`（默认，兼容现状）或 `huawei` |
| appid | 华为 AGC 应用的 Client ID |
| project_id | AGC 项目 ID（华为 v3 场景化接口路径中需要） |
| service_account_json | 服务账号 JSON（含 client_email、private_key、key_id 等），仅服务端保存 |

注意：`token` 字段语义随渠道变化——jpush 渠道下是极光 Alias，huawei 渠道下是华为 Push Token。
表单中要写清这一点，避免用户填错。

## 3. 服务端实现要点（huawei 渠道）

1. 鉴权：HarmonyOS 5 及以上，华为推送服务端**不再支持 OAuth 2.0 client_credentials 客户端模式**，
   必须使用**服务账号（Service Account）JWT** 换取访问令牌。用服务账号私钥签 JWT，再换取
   access_token，调用推送接口时带 `Authorization: Bearer <access_token>`。
2. 发送接口：华为场景化消息接口 `/v3/{projectId}/messages:send`，目标使用 `target.token`
   （数组，填配置中的 Push Token）。
3. 请求体：按华为“场景化消息”请求结构组织（payload + target + pushOptions），
   通知类消息至少包含标题与正文。**具体字段名与取值必须以官方文档当次版本为准**，
   不要凭记忆硬编码。
4. 令牌缓存：access_token 有有效期（官方建议 JWT 有效期约 3600 秒），
   要在插件内存中缓存并做提前刷新，避免每条推送都重新换令牌。
5. 错误处理：区分鉴权失败、token 非法、限流等错误，把华为返回的错误码与消息记录到
   插件日志与历史记录中；失败不得抛穿事件处理链路。
6. 安全：服务账号 JSON 与 private_key 只存插件配置，日志与页面展示必须脱敏，
   不得出现在 /run 响应里。

### 待核对清单（开发时必须逐条对照官方文档）

- [ ] 服务账号 JWT 的签名算法、claims 字段与换取令牌的端点
- [ ] v3 场景化消息的完整请求体结构与字段枚举
- [ ] 通知消息与数据消息（BACKGROUND）在接口上的差异
- [ ] 各类错误码含义与重试策略

## 4. 兼容与回归要求

- jpush 渠道行为保持不变，不能因为新增渠道而破坏现有用户配置。
- 现有 dashboard、历史记录、统计、通知类型过滤、测试内容自定义等功能对两个渠道都适用。
- V2 与 V3 两份源码必须完全一致（AGENTS.md 要求），版本号、package 索引、history 顶部同步递增。
- 测试：在 `tests/v3/apppushmsg/` 补华为渠道的用例（鉴权构造、渠道分发、错误处理），
  并保证原有 JPush 用例继续通过。

## 5. 交付标准

- [ ] 真实 MoviePilot V3 宿主可加载插件，配置页正确显示渠道与华为字段。
- [ ] 切换到 huawei 渠道并填好 appid / project_id / service_account_json / token 后，
      调用 `/api/v1/plugin/AppPushMsg/run?apikey=...` 能让鸿蒙设备收到测试通知，且返回 code=0。
- [ ] 未配置或配置错误时返回可读的错误信息，且不泄露私钥。
- [ ] 插件日志中不出现 private_key 明文。
