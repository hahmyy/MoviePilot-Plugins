# 插件仓单测

测试统一放在仓库根 `tests/` 下，**不放在插件目录内**——插件的本地同步与市场下发按
整目录拷贝，插件目录内的测试会被一并下发到运行时副本。

## 目录结构

```
tests/
├─ _bootstrap.py   薄壳 shim：定位 MoviePilot 后端入 sys.path，引导逻辑委托主程序 app/testing.bootstrap
├─ conftest.py     pytest 引导：按本次运行目标选择 v1/v3 插件环境并注册网络守卫
├─ v1/             经典 plugins/（V2 兼容）实现的单测；每个插件按插件 ID 建子目录
└─ v3/             plugins.v3/（V3 专用）实现的单测；每个插件按插件 ID 建子目录
```

说明：测试目录的代际对应的是**源码目录**，不是 MoviePilot 版本号。V2 兼容插件
同时维护 `plugins.v2/`（版本化索引）与 `plugins/`（经典回退索引）两份等价源码，
测试以经典 `plugins/` 副本为准，放在 `tests/v1/`；测试内还会校验两份源码与
`package.json` / `package.v2.json` 元数据一致。

## 运行

需要 MoviePilot 后端源码与依赖。后端放在插件仓**同级目录**（目录名 MoviePilot），
或用环境变量 `MOVIEPILOT_BACKEND_PATH` 指向后端根目录，再用带后端依赖的解释器运行
（例如 `<后端>/.venv/Scripts/python.exe`）：

```
# 全量回归（ci/v1/v3 各自独立子进程运行）
<python> tests/run.py

# 只跑某个插件某代
<python> -m pytest tests/v1/moviepilotapppush
<python> -m pytest tests/v3/moviepilotapppush
```

经典 `plugins/` 实现的兼容回归在 V3 后端的 v1 兼容会话中进行（与官方 CI 的做法
一致）；V2 宿主内的真实加载由宿主运行时验证。

隔离 CONFIG_DIR、建表、站点资源垫片、插件目录注入和网络守卫等引导逻辑统一在主程序
`app/testing` 维护一处；本仓 `tests/_bootstrap.py` 只是「定位后端入 sys.path」的薄壳。
后端需为含 `app/testing/bootstrap` 的较新 MoviePilot。

测试必须通过生产命名空间 `app.plugins.<plugin_id>` 导入插件，不要把插件目录加入
`sys.path` 后使用顶层包名，避免同一源码被重复执行、事件订阅或类状态重复创建。

## 新增用例

1. 放到 `tests/<v1|v3>/<plugin_id>/`，文件名使用 `test_*.py`，目录内不再重复插件名前缀；
2. 使用 `app.plugins.<plugin_id>` 生产路径导入插件；
3. 使用 pytest 风格（普通函数 + assert），不用 unittest 组织；
4. 优先用 `object.__new__` 绕过插件 `__init__` 只测纯逻辑，避免依赖完整运行时；
5. 提交前运行受影响插件测试和版本门禁（见仓库根 README）。