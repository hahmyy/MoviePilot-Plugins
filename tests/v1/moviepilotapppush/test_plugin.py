# -*- coding: utf-8 -*-
"""MoviePilotAppPush V2 导入、版本线、接口合同与事件转发测试。

V2 兼容插件放在经典 plugins/ 目录（官方约定由 tests/v1 代会话承载），测试在 V3 后端的兼容会话中运行（conftest 注入
plugins/），与上游 CI 对经典兼容插件的回归方式一致。
"""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins/moviepilotapppush/__init__.py"
MANIFEST = ROOT / "package.json"


def _load_plugin():
    """用生产命名空间导入插件（conftest 已注入 plugins/）。"""
    return importlib.import_module("app.plugins.moviepilotapppush")


def _new_instance(module, config=None):
    """绕过宿主 Chain 运行上下文，只测插件自身逻辑。"""
    plugin = object.__new__(module.MoviePilotAppPush)
    plugin.init_plugin(config or {})
    return plugin


def test_manifest_and_plugin_are_v2_aligned() -> None:
    """V2 索引、源码版本、图标与 V2 SDK 入口保持一致。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["MoviePilotAppPush"]
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert manifest["version"] == "0.1.8"
    assert 'plugin_version = "0.1.8"' in source
    assert manifest["icon"] == "MoviePilotAppPush.png"
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
    assert apis[0]["path"] == "/MoviePilotAppPush/run"
    assert apis[0]["auth"] == "bear"


def test_stop_service_is_idempotent() -> None:
    """停用可重复调用，热重载/卸载不残留后台资源。"""
    module = _load_plugin()
    plugin = _new_instance(module, {})
    plugin.stop_service()
    plugin.stop_service()


def test_v2_layouts_stay_identical() -> None:
    """经典 plugins/ 与版本化 plugins.v2/ 的 V2 实现必须保持一致。"""
    classic = (ROOT / "plugins/moviepilotapppush/__init__.py").read_bytes()
    versioned = (ROOT / "plugins.v2/moviepilotapppush/__init__.py").read_bytes()
    assert classic == versioned

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["MoviePilotAppPush"]
    versioned_package = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))["MoviePilotAppPush"]
    assert package["version"] == versioned_package["version"] == "0.1.8"
    assert package["icon"] == versioned_package["icon"] == "MoviePilotAppPush.png"


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


def test_dashboard_meta_and_message_type_filter() -> None:
    """仪表盘有元信息入口；消息类型筛选只放行选中的类型。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {"enabled": True, "token": "alias-device-1", "msgtypes": ["Subscribe"]},
    )
    meta = plugin.get_dashboard_meta()
    assert meta and meta[0]["key"] == "moviepilotapppush_dashboard" and meta[0]["name"]

    form, defaults = plugin.get_form()
    form_text = json.dumps(form, ensure_ascii=False)
    assert '"model": "msgtypes"' in form_text
    assert defaults["msgtypes"] == []

    calls = []

    def fake_push(title, text, extras=None):
        calls.append((title, text))
        return True, "ok"

    plugin._push = fake_push
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
    assert not calls, "未选中的消息类型应跳过"

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
    assert len(calls) == 1 and calls[0][0] == "订阅完成"


def test_custom_test_content_and_onlyonce_trigger() -> None:
    """测试内容可自定义，保存“立即发送”开关时触发一次并自动复位。"""
    module = _load_plugin()
    plugin = _new_instance(
        module,
        {
            "enabled": True,
            "apikey": "k",
            "token": "t",
            "appkey": "ak",
            "mastersecret": "ms",
            "testtitle": "自定义标题",
            "testtext": "自定义内容",
        },
    )
    form, defaults = plugin.get_form()
    form_text = json.dumps(form, ensure_ascii=False)
    assert '"model": "testtitle"' in form_text
    assert '"model": "testtext"' in form_text
    assert '"model": "onlyonce"' in form_text
    assert defaults["onlyonce"] is False

    calls = []
    plugin._push = lambda title, text, extras=None: (calls.append((title, text)) or (True, "ok"))
    plugin.run(apikey="k")
    assert calls[-1] == ("自定义标题", "自定义内容")

    calls.clear()
    saved = {}
    plugin.update_config = lambda cfg, plugin_id=None: saved.update(cfg)
    plugin._send_test_now = lambda: calls.append("sent")
    plugin.init_plugin(
        {
            "enabled": True,
            "apikey": "k",
            "token": "t",
            "appkey": "ak",
            "mastersecret": "ms",
            "onlyonce": True,
            "testtitle": "x",
            "testtext": "y",
        }
    )
    assert saved.get("onlyonce") is False
    assert calls == ["sent"]


# --------------------------------------------------------------------- #
# 华为 Push Kit 直连渠道测试
# --------------------------------------------------------------------- #
import base64 as _b64
import functools as _functools

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives import serialization as _serialization
from cryptography.hazmat.primitives.asymmetric import padding as _padding
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa


@_functools.lru_cache(maxsize=1)
def _service_account():
    """生成测试用服务账号（RSA 私钥仅存在于测试内存）。"""
    key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        _serialization.Encoding.PEM,
        _serialization.PrivateFormat.PKCS8,
        _serialization.NoEncryption(),
    ).decode("utf-8")
    return {
        "key_id": "kid-test",
        "sub_account": "sub-test",
        "private_key": pem,
        "token_uri": "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
        "project_id": "proj-test",
    }


def _decode_segment(segment: str):
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(_b64.urlsafe_b64decode(padded).decode("utf-8"))


class _FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_huawei_service_account_jwt_matches_official_contract() -> None:
    """服务账号 JWT：PS256、kid/typ/alg 与 aud/iss/iat/exp 符合官方文档。"""
    module = _load_plugin()
    account = _service_account()
    token = module._build_service_account_jwt(account, now=1700000000)
    header_seg, payload_seg, signature_seg = token.split(".")
    header = _decode_segment(header_seg)
    claims = _decode_segment(payload_seg)
    assert header == {"kid": "kid-test", "typ": "JWT", "alg": "PS256"}
    assert claims["aud"] == account["token_uri"]
    assert claims["iss"] == "sub-test"
    assert claims["iat"] == 1700000000 and claims["exp"] - claims["iat"] == 3600

    key = _serialization.load_pem_private_key(account["private_key"].encode("utf-8"), password=None)
    signature = _b64.urlsafe_b64decode(signature_seg + "=" * (-len(signature_seg) % 4))
    key.public_key().verify(
        signature,
        "{}.{}".format(header_seg, payload_seg).encode("ascii"),
        _padding.PSS(mgf=_padding.MGF1(_hashes.SHA256()), salt_length=_hashes.SHA256().digest_size),
        _hashes.SHA256(),
    )


def test_huawei_payload_and_response_parsing() -> None:
    """v3 请求体结构与响应码解析符合官方文档。"""
    module = _load_plugin()
    payload = module._build_huawei_payload("标题", "正文", "MARKETING", "TOKEN1")
    notification = payload["payload"]["notification"]
    assert notification["title"] == "标题" and notification["body"] == "正文"
    assert notification["category"] == "MARKETING"
    assert payload["target"] == {"token": ["TOKEN1"]}
    assert payload["pushOptions"]["testMessage"] is False
    fallback = module._build_huawei_payload("t", "b", "UNKNOWN", "T")
    assert fallback["payload"]["notification"]["category"] == "MARKETING"

    ok, message = module._parse_huawei_response(200, {"code": "80000000", "msg": "Success", "requestId": "R1"})
    assert ok and "R1" in message
    ok, message = module._parse_huawei_response(401, {"code": "80200001", "msg": "Authentication failed", "requestId": "R2"})
    assert not ok and "80200001" in message and "Authentication failed" in message


def test_huawei_channel_uses_access_token_and_v3_request(monkeypatch) -> None:
    """huawei 渠道先换 access_token，再按 v3 契约发送。"""
    module = _load_plugin()
    account = _service_account()
    plugin = _new_instance(
        module,
        {
            "enabled": True,
            "apikey": "k",
            "channel": "huawei",
            "token": "TOKEN1",
            "project_id": "proj-test",
            "appkey": "",
            "mastersecret": "",
        },
    )
    plugin._hw_service_account = account
    calls = []

    class FakeRequestUtils:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if "oauth2" in url:
                return _FakeResponse(200, {"access_token": "AT1", "expires_in": 3600})
            return _FakeResponse(200, {"code": "80000000", "msg": "Success", "requestId": "R1"})

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)
    ok, message = plugin._push_huawei("标题", "正文")
    assert ok and "R1" in message
    assert len(calls) == 2
    exchange = calls[0][1]["data"]
    assert exchange["grant_type"] == module.HUAWEI_JWT_BEARER_GRANT
    assert exchange["assertion"].count(".") == 2
    push_url, push_kwargs = calls[1]
    assert push_url.endswith("/v3/proj-test/messages:send")
    assert push_kwargs["headers"]["push-type"] == "0"
    assert push_kwargs["headers"]["Authorization"] == "Bearer AT1"
    assert push_kwargs["json"]["target"]["token"] == ["TOKEN1"]


def test_huawei_channel_falls_back_to_jwt(monkeypatch) -> None:
    """换 access_token 不可用时回退官方 JWT 直连，且不泄露私钥。"""
    module = _load_plugin()
    account = _service_account()
    plugin = _new_instance(
        module,
        {"enabled": True, "apikey": "k", "channel": "huawei", "token": "TOKEN1", "project_id": "proj-test"},
    )
    plugin._hw_service_account = account
    calls = []

    class FakeRequestUtils:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if "oauth2" in url:
                return _FakeResponse(401, {"error": "invalid_grant"})
            return _FakeResponse(200, {"code": "80000000", "msg": "Success", "requestId": "R1"})

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)
    ok, message = plugin._push_huawei("t", "b")
    assert ok
    auth = calls[-1][1]["headers"]["Authorization"]
    assert auth.startswith("Bearer ") and auth[len("Bearer "):].count(".") == 2
    assert account["private_key"] not in message


def test_huawei_missing_config_returns_readable_error() -> None:
    """huawei 渠道未配置时返回可读错误，且 run 响应不含私钥。"""
    module = _load_plugin()
    plugin = _new_instance(module, {"enabled": True, "apikey": "k", "channel": "huawei", "token": "TOKEN1"})
    plugin._read_data = lambda key=None, plugin_id=None: None
    result = plugin.run(apikey="k")
    assert result["code"] != 0 and "华为" in result["msg"]
    assert "private_key" not in json.dumps(result, ensure_ascii=False)

def test_form_only_shows_current_channel_fields() -> None:
    """配置页按当前保存的渠道只返回对应字段，并支持上传服务账号 JSON。"""
    module = _load_plugin()

    jpush_plugin = _new_instance(module, {"enabled": True, "channel": "jpush"})
    jform, _ = jpush_plugin.get_form()
    jmodels = [item.get("props", {}).get("model") for item in jform[0]["content"]]
    assert "appkey" in jmodels and "mastersecret" in jmodels
    assert "project_id" not in jmodels and "service_account_json" not in jmodels

    huawei_plugin = _new_instance(module, {"enabled": True, "channel": "huawei"})
    hform, defaults = huawei_plugin.get_form()
    components = hform[0]["content"]
    hmodels = [item.get("props", {}).get("model") for item in components]
    assert "project_id" in hmodels and "service_account_json" in hmodels
    assert "appkey" not in hmodels and "mastersecret" not in hmodels

    file_inputs = [item for item in components if item.get("component") == "VFileInput"]
    assert len(file_inputs) == 1
    handler = file_inputs[0]["props"]["onUpdate:modelValue"]
    assert "service_account_json" in handler and "service_account_file" in handler
    assert defaults["service_account_json"] == "" and defaults["service_account_file"] == ""
