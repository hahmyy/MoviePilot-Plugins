# MoviePilot Plugins（个人 V3 插件仓）

个人维护的 MoviePilot V3 插件源码与市场索引仓库，结构与官方
MoviePilot-Plugins 保持一致，方便把开发好的插件直接放进仓库、同步到
MoviePilot 测试，或作为插件市场源使用。

## 目录约定

- `plugins.v3/<plugin_id_lower>/`：V3 插件源码，主类定义在目录的 `__init__.py`
- `package.v3.json`：V3 插件市场索引（键名为插件 ID，与类名一致）
- `icons/`：插件图标，索引里的 `icon` 字段引用这里的文件名
- `tests/v3/<plugin_id_lower>/`：每个 V3 插件的 pytest 用例
- `docs/Plugin_Development.md`：V3 插件开发主指南

对应关系（与官方一致）：

- 插件类名 = 插件 ID = package.v3.json 键名，例如 `AppPushMsg`
- 目录名 = 类名小写，例如 `plugins.v3/apppushmsg/`
- 插件类 `plugin_version`、索引 `version`、`history` 首项版本必须一致

## 当前插件

| 插件 ID | 说明 | 版本 |
|---|---|---|
| AppPushMsg | 把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送，极光 JPush v3） | 0.1.0 |

## 新增插件的步骤

1. 按 `docs/Plugin_Development.md` 在 `plugins.v3/<id_lower>/` 写插件；
2. 在 `package.v3.json` 加索引条目，保持版本一致；
3. 图标放进 `icons/`，索引 `icon` 写文件名；
4. 在 `tests/v3/<id_lower>/test_*.py` 加测试；
5. 跑版本门禁与相关测试（见下），确认通过后提交。

## 本地测试

### 方式一：作为本地插件仓直接给 MoviePilot 用

在 MoviePilot 宿主配置 `PLUGIN_LOCAL_REPO_PATHS` 指向本仓库根目录（多个用逗号分隔），
即可把 `plugins.v3/<id>/` 与 `package.v3.json` 作为本地插件源安装/同步测试；开发插件
时再配合 `PLUGIN_AUTO_RELOAD=true`、`DEBUG=true` 使用。

### 方式二：把仓库作为插件市场

MoviePilot 只读取 GitHub 仓库的 main 分支；把本仓库推送到 GitHub 后，在 MoviePilot
插件市场配置里把仓库地址加入 `PLUGIN_MARKET`（多个用逗号分隔），即可从市场安装。

### 方式三：跑插件测试

测试需要 MoviePilot 后端源码与依赖。后端放在本仓库同级目录（目录名 MoviePilot），
或通过环境变量 `MOVIEPILOT_BACKEND_PATH` 指定后端根目录，然后用带后端依赖的解释器：

```
# 版本门禁（校验索引与插件类版本一致）
<python> .github/scripts/check_plugin_versions.py package.v3.json

# V3 全量回归
<python> tests/run.py

# 只跑单个插件
<python> -m pytest tests/v3/apppushmsg
```

更详细的测试说明见 `tests/README.md`。