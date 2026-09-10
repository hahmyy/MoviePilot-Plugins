# MoviePilot 插件开发约定

开发或维护插件时，必须先阅读 `docs/Plugin_Development.md`，并以该 V3 指南及当前
MoviePilot 宿主代码和测试为准；V2 兼容实现以 V2 宿主 SDK 与官方仓库既有实现为准。

## 目录与索引

- V2 兼容插件同时维护两份布局，源码必须完全一致（tests/v1 内有校验用例）：
  - `plugins.v2/<id_lower>/` + `package.v2.json` 条目（版本化索引，V2.15.6 优先读取）
  - `plugins/<id_lower>/` + `package.json` 条目，声明 `"v2": true`；有 V3 副本时
    再声明 `"v3": false`（经典回退索引，兼容旧 V2 宿主）
- V3 专用插件放在 `plugins.v3/<id_lower>/`，索引写入 `package.v3.json`。
- 插件类名、目录名和索引 ID 必须保持对应，例如 `AppPushMsg` 对应 `apppushmsg/`。
- 插件版本、对应索引版本和 `history` 顶部版本必须一致；同一插件同时有 V2/V3 时，
  V3 主版本为 V2 主版本 + 1。

## 代码约定

- V3 新代码优先使用 `app.sdk` 稳定入口，不新增 `app.core`、`app.helper`、`app.utils`
  等旧路径依赖；V2 兼容实现按其宿主 SDK 使用 `app.core.event`、`app.log`、
  `app.utils.http` 等入口。
- 配置、插件数据和资源必须使用基类接口或插件数据目录，不写入源码目录。
- 初始化必须可重复，停用和重载必须释放后台资源。

## 测试与提交

- 经典 `plugins/` 实现的测试放 `tests/v1/<id_lower>/`，V3 实现放
  `tests/v3/<id_lower>/`，测试导入必须走生产命名空间 `app.plugins.<plugin_id>`。
- 提交前运行该插件的对应代测试与版本门禁（`package.json`、`package.v2.json`、
  `package.v3.json` 一并校验）。