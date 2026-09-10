# -*- coding: utf-8 -*-
"""AppPushMsg V3 导入、版本线、接口合同与事件转发测试。"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins.v3/apppushmsg/__init__.py"
MANIFEST = ROOT / "package.v3.json"


def _load_plugin():
    """用生产命名空间导入插件（conftest 已注入 plugins.v3）。"""
    return importlib.import_module("app.plugins.apppushmsg")


def _new_instance(module, config=None):
    """绕过宿主 Chain 运行上下文，只测插件自身逻辑。"""
    plugin = object.__new__(module.AppPushMsg)
    plugin.init_plugin(config or {})
    return plugin


def test_manifest_and_plugin_are_v3_aligned() -> None:
    """V3 索引、源码版本、图标与稳定 SDK 入口保持一致，且无旧路径导入。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["AppPushMsg"]
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert manifest["version"] == "1.0.4"
    assert manifest["system_version"] == ">=3.0.0"
    assert 'plugin_version = "1.0.4"' in source
    assert manifest["icon"] == "AppPushMsg.png"
    assert (ROOT / "icons" / manifest["icon"]).is_file()
    assert not any(
        module.startswith(("app.core.", "app.helper.", "app.utils.", "app.log"))
        for module in imports
    )
    assert "app.sdk.network" in imports
    assert "app.sdk.events" in imports


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

    missing_key = asyncio.run(plugin.run())
    assert missing_key["code"] != 0 and "Push Key" in missing_key["msg"]

    wrong_key = asyncio.run(plugin.run(apikey="wrong"))
    assert wrong_key["code"] != 0 and "Push Key" in wrong_key["msg"]

    valid_key = asyncio.run(plugin.run(apikey="test-key"))
    assert set(valid_key) == {"code", "msg"}
    assert valid_key["code"] != 0 and "JPush" in valid_key["msg"]

    # 未启用时直接拒绝
    plugin._enabled = False
    disabled = asyncio.run(plugin.run(apikey="test-key"))
    assert disabled["code"] != 0


def test_notice_message_forwards_title_text_and_extras(monkeypatch) -> None:
    """NoticeMessage 异步转发：携带 title/text，并把事件元信息放入 extras。"""
    module = _load_plugin()
    plugin = _new_instance(
        module, {"enabled": True, "token": "alias-device-1"}
    )

    calls = []

    async def fake_push(title, text, extras=None):
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
    asyncio.run(plugin._on_notice(event))

    assert calls and calls[0][0] == "订阅完成" and calls[0][1] == "下载完成"
    extras = calls[0][2]
    assert extras.get("channel") == "telegram" and extras.get("userid") == "1"

    calls.clear()
    empty = module.Event(
        event_type=module.EventType.NoticeMessage, event_data={"text": ""}
    )
    asyncio.run(plugin._on_notice(empty))
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

    async def fake_save(key, value, plugin_id=None):
        store[key] = value

    plugin.async_save_data = fake_save
    plugin.get_data = lambda key=None, plugin_id=None: store.get(key)

    empty_page = plugin.get_page()
    assert empty_page and empty_page[0]["component"] == "VAlert"

    result = asyncio.run(plugin.run(apikey="test-key"))
    assert result["code"] != 0 and "JPush" in result["msg"]
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

    async def fake_save(key, value, plugin_id=None):
        store[key] = value

    async def fake_push(title, text, extras=None):
        return True, "推送成功，msg_id=m1"

    plugin.async_save_data = fake_save
    plugin.get_data = lambda key=None, plugin_id=None: store.get(key)
    plugin._push = fake_push

    cols, attrs, elements = plugin.get_dashboard()
    assert cols["cols"] == 12 and attrs["refresh"]
    assert "暂无推送记录" in json.dumps(elements, ensure_ascii=False)

    asyncio.run(plugin.run(apikey="test-key"))
    stats = store.get("push_stats")
    assert stats["push_total"] == 1 and stats["push_success"] == 1
    assert stats["test_calls"] == 1
    assert store["push_history"][0]["kind"] == "测试"

    asyncio.run(
        plugin._on_notice(
            module.Event(
                event_type=module.EventType.NoticeMessage,
                event_data={"title": "订阅完成", "text": "下载完成"},
            )
        )
    )
    stats = store.get("push_stats")
    assert stats["push_total"] == 2 and stats["notice_total"] == 1

    dashboard_text = json.dumps(plugin.get_dashboard()[2], ensure_ascii=False)
    assert "历史消息" in dashboard_text and "订阅完成" in dashboard_text


def test_dashboard_meta_and_message_type_filter() -> None:
    """仪表盘有元信息入口；消息类型筛选只放行选中的类型。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {"enabled": True, "token": "alias-device-1", "msgtypes": ["Subscribe"]},
    )
    meta = plugin.get_dashboard_meta()
    assert meta and meta[0]["key"] == "apppushmsg_dashboard" and meta[0]["name"]

    form, defaults = plugin.get_form()
    assert '"model": "msgtypes"' in json.dumps(form, ensure_ascii=False)
    assert defaults["msgtypes"] == []

    calls = []

    async def fake_push(title, text, extras=None):
        calls.append((title, text))
        return True, "ok"

    plugin._push = fake_push
    asyncio.run(
        plugin._on_notice(
            module.Event(
                event_type=module.EventType.NoticeMessage,
                event_data={
                    "title": "站点消息",
                    "text": "x",
                    "type": module.NotificationType.Other,
                },
            )
        )
    )
    assert not calls, "未选中的消息类型应跳过"

    asyncio.run(
        plugin._on_notice(
            module.Event(
                event_type=module.EventType.NoticeMessage,
                event_data={
                    "title": "订阅完成",
                    "text": "y",
                    "type": module.NotificationType.Subscribe,
                },
            )
        )
    )
    assert len(calls) == 1 and calls[0][0] == "订阅完成"
