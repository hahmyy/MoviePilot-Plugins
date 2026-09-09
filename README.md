# MoviePilot Plugins（个人插件仓）

个人维护的 MoviePilot 插件源码与市场索引仓库，结构与官方
MoviePilot-Plugins 保持一致，方便把开发好的插件直接放进仓库、同步到
MoviePilot 测试，或作为插件市场源使用。

## 目录约定

- `plugins.v3/<plugin_id_lower>/`：MoviePilot V3 插件源码（v3 当前内测）
- `plugins.v2/<plugin_id_lower>/`：MoviePilot V2 插件源码（当前主力使用的版本）
- `package.v3.json`：V3 插件市场索引
- `package.v2.json`：V2 插件市场索引（V2 宿主优先读取，其次回退 package.json）
- `icons/`：插件图标，索引里的 `icon` 字段引用这里的文件名
- `tests/v3/<plugin_id_lower>/`、`tests/v2/<plugin_id_lower>/`：对应代插件测试
- `docs/Plugin_Development.md`：V3 插件开发主指南

对应关系（与官方一致）：

- 插件类名 = 插件 ID = 对应 package*.json 键名，例如 `AppPushMsg`
- 目录名 = 类名小写，例如 `plugins.v3/apppushmsg/`、`plugins.v2/apppushmsg/`
- 插件类 `plugin_version`、对应索引 `version`、`history` 首项版本必须一致
- 同一插件同时提供 V2/V3 时，V3 主版本须为 V2 主版本 + 1（官方版本门禁规则）

## 当前插件

| 插件 ID | 版本线 | 说明 |
|---|---|---|
| AppPushMsg | V2 0.1.0 / V3 1.0.0 | 把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送，极光 JPush v3） |

## 新增/维护插件的步骤

1. 按对应开发指南写插件：V3 见 `docs/Plugin_Development.md`，V2 按宿主
   V2 SDK（app.core.event / app.log / app.utils.http 等）并参考 `plugins.v2/` 既有实现；
2. 在对应 `package*.json` 加索引条目，保持版本一致；
3. 图标放进 `icons/`，索引 `icon` 写文件名（V2/V3 可共用）；
4. 在 `tests/v3|v2/<plugin_id_lower>/test_*.py` 加测试；
5. 跑版本门禁与相关测试（见下），确认通过后提交。

## 本地测试

### 方式一：作为本地插件仓直接给 MoviePilot 用

在 MoviePilot 宿主配置 `PLUGIN_LOCAL_REPO_PATHS` 指向本仓库根目录（多个用逗号分隔），
即可把对应 `plugins.v2|v3/<id>/` 与 `package*.json` 作为本地插件源安装/同步测试；开发插件
时再配合 `PLUGIN_AUTO_RELOAD=true`、`DEBUG=true` 使用。

### 方式二：把仓库作为插件市场

MoviePilot 只读取 GitHub 仓库的 main 分支；把本仓库推送到 GitHub 后，在 MoviePilot
插件市场配置里把仓库地址加入 `PLUGIN_MARKET`（多个用逗号分隔），即可从市场安装。
V2 宿主会优先读取 `package.v2.json` 与 `plugins.v2/`。

### 方式三：跑插件测试

测试需要 MoviePilot 后端源码与依赖。后端放在本仓库同级目录（目录名 MoviePilot），
或通过环境变量 `MOVIEPILOT_BACKEND_PATH` 指定后端根目录，然后用带后端依赖的解释器：

```
# 版本门禁（校验 V2/V3 索引与插件类版本一致）
<python> .github/scripts/check_plugin_versions.py package.v2.json package.v3.json

# 全量回归（v2/v3 分独立会话运行）
<python> tests/run.py

# 只跑单个插件某代测试
<python> -m pytest tests/v2/apppushmsg
<python> -m pytest tests/v3/apppushmsg
```

更详细的测试说明见 `tests/README.md`。