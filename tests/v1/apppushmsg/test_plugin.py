# -*- coding: utf-8 -*-
"""AppPushMsg V2 导入、版本线、接口合同与事件转发测试。

V2 兼容插件放在经典 plugins/ 目录（官方约定由 tests/v1 代会话承载），测试在 V3 后端的兼容会话中运行（conftest 注入
plugins/），与上游 CI 对经典兼容插件的回归方式一致。
"""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins/apppushmsg/__init__.py"
MANIFEST = ROOT / "package.json"


def _load_plugin():
    """用生产命名空间导入插件（conftest 已注入 plugins/）。"""
    return importlib.import_module("app.plugins.apppushmsg")


def _new_instance(module, config=None):
    """绕过宿主 Chain 运行上下文，只测插件自身逻辑。"""
    plugin = object.__new__(module.AppPushMsg)
    plugin.init_plugin(config or {})
    return plugin


def test_manifest_and_plugin_are_v2_aligned() -> None:
    """V2 索引、源码版本、图标与 V2 SDK 入口保持一致。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["AppPushMsg"]
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert manifest["version"] == "0.1.2"
    assert 'plugin_version = "0.1.2"' in source
    assert manifest["icon"] == "AppPushMsg.png"
    assert (ROOT / "icons" / manifest["icon"]).is_file()
    assert "app.core.event" in imports
    assert "app.log" in imports
    assert "app.utils.http" in imports
    assert not any(module.startswith(("app.sdk",)) for module in imports)


def test_run_contract_validates_apikey_and_credentials_without_network() -> None:
    """测试接口返回 code/msg；key 与极光凭据校验在发请求前完成。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {
            "enabled": True,
            "apikey": "test-key",
            "token": "alias-device-1",
            "appkey": "",
            "mastersecret": "",
        },
    )

    missing_key = plugin.run()
    assert missing_key["code"] != 0 and "Push Key" in missing_key["msg"]

    wrong_key = plugin.run(apikey="wrong")
    assert wrong_key["code"] != 0 and "Push Key" in wrong_key["msg"]

    valid_key = plugin.run(apikey="test-key")
    assert set(valid_key) == {"code", "msg"}
    assert valid_key["code"] != 0 and "JPush" in valid_key["msg"]

    plugin._enabled = False
    disabled = plugin.run(apikey="test-key")
    assert disabled["code"] != 0


def test_notice_message_forwards_title_text_and_extras(monkeypatch) -> None:
    """NoticeMessage 转发：携带 title/text，并把事件元信息放入 extras。"""
    module = _load_plugin()
    plugin = _new_instance(module, {"enabled": True, "token": "alias-device-1"})

    calls = []

    def fake_push(title, text, extras=None):
        calls.append((title, text, extras or {}))
        return True, "ok"

    monkeypatch.setattr(plugin, "_push", fake_push)
    event = module.Event(
        event_type=module.EventType.NoticeMessage,
        event_data={
            "channel": "telegram",
            "type": "notice",
            "title": "订阅完成",
            "text": "下载完成",
            "source": "test",
            "userid": 1,
        },
    )
    plugin._on_notice(event)

    assert calls and calls[0][0] == "订阅完成" and calls[0][1] == "下载完成"
    extras = calls[0][2]
    assert extras.get("channel") == "telegram" and extras.get("userid") == "1"

    calls.clear()
    empty = module.Event(
        event_type=module.EventType.NoticeMessage, event_data={"text": ""}
    )
    plugin._on_notice(empty)
    assert not calls, "空消息应跳过"


def test_api_path_is_prefixed_with_plugin_id() -> None:
    """get_api 的 path 由宿主投影为 /<PluginID>/<path>。"""
    module = _load_plugin()
    plugin = _new_instance(module, {})
    from app.runtime.extensions.plugin.projection import PluginProjection

    apis = PluginProjection({plugin.__class__.__name__: plugin}).apis(
        plugin.__class__.__name__
    )
    assert len(apis) == 1
    assert apis[0]["path"] == "/AppPushMsg/run"
    assert apis[0]["auth"] == "bear"


def test_stop_service_is_idempotent() -> None:
    """停用可重复调用，热重载/卸载不残留后台资源。"""
    module = _load_plugin()
    plugin = _new_instance(module, {})
    plugin.stop_service()
    plugin.stop_service()


def test_v2_layouts_stay_identical() -> None:
    """经典 plugins/ 与版本化 plugins.v2/ 的 V2 实现必须保持一致。"""
    classic = (ROOT / "plugins/apppushmsg/__init__.py").read_bytes()
    versioned = (ROOT / "plugins.v2/apppushmsg/__init__.py").read_bytes()
    assert classic == versioned

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["AppPushMsg"]
    versioned_package = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))["AppPushMsg"]
    assert package["version"] == versioned_package["version"] == "0.1.2"
    assert package["icon"] == versioned_package["icon"] == "AppPushMsg.png"


def test_run_records_last_result_and_page_shows_it() -> None:
    """run 记录最近一次测试结果，get_page 展示时间、目标与返回信息。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {
            "enabled": True,
            "apikey": "test-key",
            "token": "alias-device-1",
            "appkey": "",
            "mastersecret": "",
        },
    )
    store = {}
    plugin.save_data = lambda key, value, plugin_id=None: store.__setitem__(key, value)
    plugin.get_data = lambda key=None, plugin_id=None: store.get(key)

    empty_page = plugin.get_page()
    assert empty_page and empty_page[0]["component"] == "VAlert"

    plugin.run(apikey="test-key")
    saved = store.get("last_test_result")
    assert saved and saved["code"] != 0 and "JPush" in saved["msg"]
    assert "***" in saved["token"]

    page_text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "最近一次测试结果" in page_text
    assert "测试时间" in page_text and "JPush" in page_text


def test_dashboard_reports_stats_and_history() -> None:
    """仪表盘包含调用统计、连接状态与历史消息。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {
            "enabled": True,
            "apikey": "test-key",
            "token": "alias-device-1",
            "appkey": "ak",
            "mastersecret": "ms",
        },
    )
    store = {}
    plugin.save_data = lambda key, value, plugin_id=None: store.__setitem__(key, value)
    plugin.get_data = lambda key=None, plugin_id=None: store.get(key)

    cols, attrs, elements = plugin.get_dashboard()
    assert cols["cols"] == 12 and attrs["refresh"]
    assert "暂无推送记录" in json.dumps(elements, ensure_ascii=False)

    plugin._push = lambda title, text, extras=None: (True, "推送成功，msg_id=m1")
    plugin.run(apikey="test-key")
    stats = store.get("push_stats")
    assert stats["push_total"] == 1 and stats["push_success"] == 1
    assert stats["test_calls"] == 1
    assert store["push_history"][0]["kind"] == "测试"

    plugin._on_notice(
        module.Event(
            event_type=module.EventType.NoticeMessage,
            event_data={"title": "订阅完成", "text": "下载完成"},
        )
    )
    stats = store.get("push_stats")
    assert stats["push_total"] == 2 and stats["notice_total"] == 1

    dashboard_text = json.dumps(plugin.get_dashboard()[2], ensure_ascii=False)
    assert "历史消息" in dashboard_text and "订阅完成" in dashboard_text
