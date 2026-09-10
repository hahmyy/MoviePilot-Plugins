# MoviePilot Plugins（个人插件仓）

个人维护的 MoviePilot 插件源码与市场索引仓库，结构与官方
MoviePilot-Plugins 保持一致，方便把开发好的插件直接放进仓库、同步到
MoviePilot 测试，或作为插件市场源使用。

## V2 兼容插件的双布局（重要）

MoviePilot V2（v2.15.6 及同代）插件市场会同时读取两份索引并合并：

- 版本化索引 `package.v2.json`（对应源码目录 `plugins.v2/`，优先路径）
- 经典索引 `package.json`（对应源码目录 `plugins/`，V2 兼容回退路径；
  条目必须声明 `"v2": true`，若已有 V3 副本再加 `"v3": false`）

为了让新旧 V2 宿主、以及配置了 GitHub 镜像/缓存的环境都能稳定获取，本仓的 V2
兼容插件同时提供两份布局，源码必须保持一致（测试会校验两份文件完全相同）：

- `plugins.v2/apppushmsg/` + `package.v2.json` 的 `AppPushMsg` 条目
- `plugins/apppushmsg/` + `package.json` 的 `AppPushMsg` 条目（v2 为 true、v3 为 false）

V3 专用实现仍放 `plugins.v3/<plugin_id_lower>/` 与 `package.v3.json`。

## 目录约定

- `plugins/<plugin_id_lower>/`：经典/跨版本实现，V2 宿主回退安装（package.json）
- `plugins.v2/<plugin_id_lower>/`：V2 版本化实现（package.v2.json）
- `plugins.v3/<plugin_id_lower>/`：MoviePilot V3 专用插件（v3 当前内测）
- `package.json`：默认/经典索引，V2 兼容实现声明 `"v2": true`（有 V3 副本时加 `"v3": false`）
- `package.v2.json`：V2 版本化索引
- `package.v3.json`：V3 插件市场索引
- `icons/`：插件图标，索引里的 `icon` 字段引用这里的文件名（各代可共用）
- `tests/v1/`：经典 `plugins/` 实现的测试；`tests/v3/`：V3 实现测试

对应关系（与官方一致）：

- 插件类名 = 插件 ID = package*.json 键名，例如 `AppPushMsg`
- 目录名 = 类名小写，例如 `plugins/apppushmsg/`、`plugins.v2/apppushmsg/`、`plugins.v3/apppushmsg/`
- 插件类 `plugin_version`、对应索引 `version`、`history` 首项版本必须一致
- 同一插件同时提供 V2/V3 时，V3 主版本须为 V2 主版本 + 1（官方版本门禁规则）

## 当前插件

| 插件 ID | 版本线 | 说明 |
|---|---|---|
| AppPushMsg | V2 兼容 0.1.2（plugins/ 与 plugins.v2/ 双份）/ V3 1.0.2 | 把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送，极光 JPush v3）；插件详情页展示最近一次测试结果；首页仪表盘展示调用次数、连接状态与历史消息 |

## 新增/维护插件的步骤

1. V2 兼容实现：写 `plugins.v2/<id_lower>/` 与 `plugins/<id_lower>/` 两份等价源码，
   分别补 `package.v2.json` 与 `package.json`（v2: true、v3: false）条目；
2. V3 专用实现：按 `docs/Plugin_Development.md` 写 `plugins.v3/<id_lower>/`，
   并在 `package.v3.json` 加条目（`system_version: ">=3.0.0"`）；
3. 图标放进 `icons/`，索引 `icon` 写文件名；
4. 在 `tests/v1/<id_lower>/`（经典实现）或 `tests/v3/<id_lower>/`（V3）加测试，
   并校验两份 V2 源码一致；
5. 跑版本门禁与相关测试（见下），确认通过后提交。

## 本地测试

### 方式一：作为本地插件仓直接给 MoviePilot 用

在 MoviePilot 宿主配置 `PLUGIN_LOCAL_REPO_PATHS` 指向本仓库根目录（多个用逗号分隔），
V2 宿主会用 `package.v2.json` + `plugins.v2/`（优先）或 `package.json` + `plugins/`
（回退）安装，V3 宿主会用 `package.v3.json` + `plugins.v3/` 安装；开发时再配合
`PLUGIN_AUTO_RELOAD=true`、`DEBUG=true` 使用。

### 方式二：把仓库作为插件市场

MoviePilot 读取 GitHub 仓库的 main 分支；把本仓库推送到 GitHub 后，在 MoviePilot
插件市场配置里把仓库地址加入 `PLUGIN_MARKET`（多个用逗号分隔）。地址使用
`https://github.com/hahmyy/MoviePilot-Plugins` 形式，不要带 `.git` 或 `/tree/main`。

若配置了 `GITHUB_PROXY`（GitHub 镜像站），镜像可能缓存旧结果；请在插件市场里执行
一次强制刷新（会带 `_refresh` 时间戳绕过缓存），必要时清理镜像/Redis 缓存或暂时直连。

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