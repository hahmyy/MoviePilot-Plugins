# -*- coding: utf-8 -*-
"""MoviePilot V2 版 AppPushMsg：把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送）。

与 plugins.v3/apppushmsg 功能一致，按 V2 宿主 SDK 实现：
- 事件与 logger 走 app.core.event / app.log；
- HTTP 走 app.utils.http.RequestUtils（requests，同步）；
- 事件处理器为同步方法，由 V2 eventmanager 放入线程池执行。
"""
from __future__ import annotations

import base64
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


def _coerce_str(value: Any) -> str:
    """把任意事件值转成可推送的字符串。"""
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip()


def _event_data_to_dict(data: Any) -> dict:
    """把广播事件携带的数据统一成字典，兼容 dict / Pydantic / 旧对象。"""
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump") and callable(data.model_dump):
        return data.model_dump(mode="python") or {}
    if hasattr(data, "to_dict") and callable(data.to_dict):
        return data.to_dict() or {}
    if hasattr(data, "__dict__"):
        return dict(data.__dict__)
    return {}


def _build_extras(event_data: dict) -> dict:
    """从 NoticeMessage 事件数据中提取可下发的扩展字段。"""
    extras: Dict[str, str] = {}
    for key in ("channel", "type", "source", "userid"):
        value = event_data.get(key)
        if value is None:
            continue
        text = _coerce_str(value)
        if text:
            extras[key] = text
    return extras


def _compose_notification(title: str, text: str, extras: dict) -> dict:
    """按极光 v3 结构拼装多平台通知体。

    - 标题、正文都有时：android/hmos 使用系统标题 + 正文，iOS 由于 APNs 只有
      alert，把标题拼在正文前保证不丢信息。
    - 只有标题或只有正文时：统一走 notification.alert。
    - hmos 按厂商通道规范带 category（IM），鸿蒙厂商通道必填。
    """
    title = _coerce_str(title)
    text = _coerce_str(text)
    if title and text:
        return {
            "alert": text,
            "android": {"alert": text, "title": title, "extras": extras},
            "ios": {"alert": "{}\n{}".format(title, text), "extras": extras},
            "hmos": {
                "alert": text,
                "title": title,
                "category": "IM",
                "extras": extras,
            },
        }
    return {"alert": title or text, "extras": extras}


class AppPushMsg(_PluginBase):
    """把 MoviePilot 通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。

    设计约定：
    - 插件 ID 必须为 AppPushMsg（App 端据此渲染专用配置页并调用 /run 测试接口）。
    - App Push Token 即极光 Alias，App 端点“应用”后写入设备。
    - 服务端凭据 appkey / mastersecret 只在服务端配置，不下发、不打印。
    """

    # 插件名称
    plugin_name = "App 推送"
    # 插件描述
    plugin_desc = "将 MoviePilot 通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。"
    # 插件图标
    plugin_icon = "AppPushMsg.png"
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "hahmyy"
    # 作者主页
    author_url = "https://github.com/hahmyy"
    # 插件配置项ID前缀
    plugin_config_prefix = "apppushmsg_"
    # 加载顺序
    plugin_order = 50
    # 可使用的用户级别
    auth_level = 1

    JPUSH_PUSH_URL = "https://api.jpush.cn/v3/push"

    _enabled = False
    _apikey = ""
    _token = ""
    _appkey = ""
    _mastersecret = ""

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._apikey = str(config.get("apikey") or "")
        self._token = str(config.get("token") or "").strip()
        self._appkey = str(config.get("appkey") or "").strip()
        self._mastersecret = str(config.get("mastersecret") or "").strip()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        # 本插件不创建后台线程/长连接，无需额外清理。
        pass

    # ------------------------------------------------------------------ #
    # 页面与配置
    # ------------------------------------------------------------------ #
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面，返回页面配置与默认数据结构。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    },
                    {
                        "component": "VTextField",
                        "props": {"model": "apikey", "label": "Push Key"},
                    },
                    {
                        "component": "VTextField",
                        "props": {"model": "token", "label": "App Push Token"},
                    },
                    {
                        "component": "VTextField",
                        "props": {"model": "appkey", "label": "JPush AppKey"},
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "mastersecret",
                            "label": "JPush Master Secret",
                            "type": "password",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "apikey": "",
            "token": "",
            "appkey": "",
            "mastersecret": "",
        }

    def get_page(self) -> List[dict]:
        return []

    # ------------------------------------------------------------------ #
    # API：App 配置页“测试”按钮调用
    # 最终路径：/api/v1/plugin/AppPushMsg/run
    # ------------------------------------------------------------------ #
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.run,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "发送一条测试推送",
                "description": "App 配置页「测试」按钮调用。校验 apikey 后，向配置的 token(Alias) 推送一条测试通知。",
            }
        ]

    def run(self, apikey: str = None) -> dict:
        """测试接口：校验 apikey 后发送一条测试通知，返回 code/msg。"""
        if not self._enabled:
            return {"code": 1, "msg": "插件未启用"}
        if not self._apikey or (apikey or "").strip() != self._apikey.strip():
            return {"code": 1, "msg": "Push Key 校验失败"}
        if not self._token:
            return {"code": 1, "msg": "未配置 App Push Token"}
        if not self._appkey or not self._mastersecret:
            return {"code": 1, "msg": "未配置 JPush 服务端凭据"}
        ok, message = self._push(
            "App 推送测试", "这是一条来自 MoviePilot 的测试通知"
        )
        return {"code": 0 if ok else 1, "msg": message}

    # ------------------------------------------------------------------ #
    # 事件：转发服务端消息通知
    # V2 eventmanager 对同步 handler 在线程池执行，方法保持同步、不做网络等待。
    # ------------------------------------------------------------------ #
    @eventmanager.register(EventType.NoticeMessage)
    def _on_notice(self, event: Event) -> None:
        if not self._enabled or not self._token:
            return
        data = _event_data_to_dict(event.event_data)
        title = _coerce_str(data.get("title"))
        text = _coerce_str(data.get("text"))
        if not title and not text:
            return
        self._push(title, text, extras=_build_extras(data))

    # ------------------------------------------------------------------ #
    # 极光推送 v3
    # ------------------------------------------------------------------ #
    def _push(
        self, title: str, text: str, extras: Optional[dict] = None
    ) -> Tuple[bool, str]:
        if not self._appkey or not self._mastersecret:
            logger.warning("AppPushMsg: 未配置 JPush AppKey / Master Secret")
            return False, "未配置 JPush 服务端凭据"

        credentials = base64.b64encode(
            "{}:{}".format(self._appkey, self._mastersecret).encode("utf-8")
        ).decode("ascii")
        headers = {"Authorization": "Basic {}".format(credentials)}
        payload = {
            "platform": ["android", "ios", "hmos"],
            "audience": {"alias": [self._token]},
            "notification": _compose_notification(title, text, extras or {}),
            # apns_production 仅影响 iOS；鸿蒙/Android 走厂商通道不受影响。
            "options": {"apns_production": True},
        }

        try:
            # RequestUtils.post(url, data=None, json=None, **kwargs) 返回
            # requests.Response；网络异常默认被吞掉并返回 None。
            response = RequestUtils().post(
                self.JPUSH_PUSH_URL, json=payload, headers=headers
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AppPushMsg 推送请求异常: {}".format(exc))
            return False, "推送请求异常: {}".format(exc)

        if response is None:
            return False, "推送网关无响应"

        status = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}

        if status < 400:
            msg_id = body.get("msg_id") or ""
            logger.info("AppPushMsg 推送成功 msg_id={}".format(msg_id))
            return True, "推送成功，msg_id={}".format(msg_id)

        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code") or status
        message = error.get("message") or "推送失败"
        logger.error("AppPushMsg 推送失败: {} {}".format(code, message))
        return False, "推送失败（{}）：{}".format(code, message)