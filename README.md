# MoviePilot Plugins（个人插件仓）

个人维护的 MoviePilot 插件源码与市场索引仓库，结构与官方
MoviePilot-Plugins 保持一致，方便把开发好的插件直接放进仓库、同步到
MoviePilot 测试，或作为插件市场源使用。

## 关键：V2 兼容插件用经典目录

MoviePilot V2（当前主力版本）市场读取规则是：

- V2 专用索引（package.v2.json）没有条目时，回退到 `package.json` 中声明
  `"v2": true` 的实现；
- 回退实现从经典 `plugins/<plugin_id_lower>/` 目录安装；
- V3 专用实现放 `plugins.v3/<plugin_id_lower>/` 与 `package.v3.json`。

所以本仓库的 V2 兼容插件放在 `plugins/apppushmsg/` + 根 `package.json`（带
`"v2": true`、`"v3": false`），`package.v2.json` 保留为空占位。这是社区仓库
（如 singleton-altman/MoviePilot-Plugins）能被 V2 宿主识别到的同款布局。

## 目录约定

- `plugins/<plugin_id_lower>/`：经典/跨版本实现，V2 宿主从这里安装（package.json）
- `plugins.v3/<plugin_id_lower>/`：MoviePilot V3 专用插件（v3 当前内测）
- `package.json`：默认/经典索引，V2 兼容实现必须声明 `"v2": true`（有 V3 副本时加 `"v3": false`）
- `package.v2.json`：V2 专用索引（本仓为空，走 package.json 回退）
- `package.v3.json`：V3 插件市场索引
- `icons/`：插件图标，索引里的 `icon` 字段引用这里的文件名（各代可共用）
- `tests/v1/`：经典 `plugins/` 实现的测试；`tests/v3/`：V3 实现测试

对应关系（与官方一致）：

- 插件类名 = 插件 ID = package*.json 键名，例如 `AppPushMsg`
- 目录名 = 类名小写，例如 `plugins/apppushmsg/`、`plugins.v3/apppushmsg/`
- 插件类 `plugin_version`、对应索引 `version`、`history` 首项版本必须一致
- 同一插件同时提供 V2/V3 时，V3 主版本须为 V2 主版本 + 1（官方版本门禁规则）

## 当前插件

| 插件 ID | 版本线 | 说明 |
|---|---|---|
| AppPushMsg | V2 兼容 0.1.0 / V3 1.0.0 | 把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送，极光 JPush v3） |

## 新增/维护插件的步骤

1. V2 兼容实现：在 `plugins/<id_lower>/` 写插件（V2 SDK：app.core.event /
   app.log / app.utils.http），并在 `package.json` 加条目、声明 `"v2": true`；
2. V3 专用实现：按 `docs/Plugin_Development.md` 写 `plugins.v3/<id_lower>/`，
   并在 `package.v3.json` 加条目（`system_version: ">=3.0.0"`）；
3. 图标放进 `icons/`，索引 `icon` 写文件名；
4. 在 `tests/v1/<id_lower>/`（经典实现）或 `tests/v3/<id_lower>/`（V3）加测试；
5. 跑版本门禁与相关测试（见下），确认通过后提交。

## 本地测试

### 方式一：作为本地插件仓直接给 MoviePilot 用

在 MoviePilot 宿主配置 `PLUGIN_LOCAL_REPO_PATHS` 指向本仓库根目录（多个用逗号分隔），
V2 宿主会用 `package.json` + `plugins/` 安装，V3 宿主会用 `package.v3.json` +
`plugins.v3/` 安装；开发时再配合 `PLUGIN_AUTO_RELOAD=true`、`DEBUG=true` 使用。

### 方式二：把仓库作为插件市场

MoviePilot 读取 GitHub 仓库的 main 分支；把本仓库推送到 GitHub 后，在 MoviePilot
插件市场配置里把仓库地址加入 `PLUGIN_MARKET`（多个用逗号分隔）。V2 宿主按
`package.json`（v2: true）识别并安装，V3 宿主按 `package.v3.json` 识别。

### 方式三：跑插件测试

测试需要 MoviePilot 后端源码与依赖。后端放在本仓库同级目录（目录名 MoviePilot），
或通过环境变量 `MOVIEPILOT_BACKEND_PATH` 指定后端根目录，然后用带后端依赖的解释器：

```
# 版本门禁（校验 V2/V3 索引与插件类版本一致）
<python> .github/scripts/check_plugin_versions.py package.json package.v2.json package.v3.json

# 全量回归（v1/v3 分独立会话运行）
<python> tests/run.py

# 只跑单个插件对应代测试
<python> -m pytest tests/v1/apppushmsg
<python> -m pytest tests/v3/apppushmsg
```

更详细的测试说明见 `tests/README.md`。