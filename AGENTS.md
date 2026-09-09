# MoviePilot 插件开发约定

开发或维护插件时，必须先阅读 `docs/Plugin_Development.md`，并以该 V3 指南及当前
MoviePilot 宿主代码和测试为准。

- 新插件放在 `plugins.v3/<plugin_id_lower>/`。
- 插件类名、目录名和 `package.v3.json` 的 ID 必须保持对应。
- 插件版本、市场索引版本和 `history` 顶部版本必须一致。
- 新代码优先使用 `app.sdk` 稳定入口，不新增 `app.core`、`app.helper`、`app.utils`
  等旧路径依赖。
- 配置、插件数据和资源必须使用基类接口或插件数据目录，不写入源码目录。
- 初始化必须可重复，停用和重载必须释放后台资源。
- 每个插件（V2/V3）必须配套 `tests/<v2|v3>/<plugin_id_lower>/test_*.py`，提交前运行该插件对应代测试与版本门禁。