from __future__ import annotations

import base64
from enum import Enum
from typing import Any

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils


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
    extras: dict[str, str] = {}
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

    - 标题、正文都有时：android/hmos 使用系统标题 + 正文，
      iOS 由于 APNs 只有 alert，把标题拼在正文前保证不丢信息。
    - 只有标题或只有正文时：统一走 notification.alert。
    - hmos 按厂商通道规范带 category（IM），鸿蒙厂商通道必填。
    """
    title = _coerce_str(title)
    text = _coerce_str(text)
    if title and text:
        return {
            "alert": text,
            "android": {"alert": text, "title": title, "extras": extras},
            "ios": {"alert": f"{title}\n{text}", "extras": extras},
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

    plugin_name = "App 推送"
    plugin_desc = "将 MoviePilot 通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。"
    plugin_icon = "AppPushMsg.png"
    plugin_version = "0.1.0"
    plugin_author = "hahmyy"
    author_url = "https://github.com/hahmyy"
    plugin_config_prefix = "apppushmsg_"
    plugin_order = 50
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
    def init_plugin(self, config: dict | None = None) -> None:
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
    def get_command() -> list[dict[str, Any]]:
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
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

    def get_page(self) -> list[dict]:
        return []

    # ------------------------------------------------------------------ #
    # API：App 配置页“测试”按钮调用
    # 最终路径：/api/v1/plugin/AppPushMsg/run
    # ------------------------------------------------------------------ #
    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.run,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "发送一条测试推送",
                "description": (
                    "App 配置页「测试」按钮调用。校验 apikey 后，"
                    "向配置的 token(Alias) 推送一条测试通知。"
                ),
            }
        ]

    async def run(self, apikey: str | None = None) -> dict[str, Any]:
        if not self._enabled:
            return {"code": 1, "msg": "插件未启用"}
        if not self._apikey or (apikey or "").strip() != self._apikey.strip():
            return {"code": 1, "msg": "Push Key 校验失败"}
        if not self._token:
            return {"code": 1, "msg": "未配置 App Push Token"}
        if not self._appkey or not self._mastersecret:
            return {"code": 1, "msg": "未配置 JPush 服务端凭据"}
        ok, message = await self._push(
            "App 推送测试", "这是一条来自 MoviePilot 的测试通知"
        )
        return {"code": 0 if ok else 1, "msg": message}

    # ------------------------------------------------------------------ #
    # 事件：转发服务端消息通知
    # 宿主 V3 eventmanager 对异步 handler 在主事件循环中 await（见
    # app/runtime/event/dispatch.py dispatch_broadcast），无需 create_task。
    # ------------------------------------------------------------------ #
    @eventmanager.register(EventType.NoticeMessage)
    async def _on_notice(self, event: Event) -> None:
        if not self._enabled or not self._token:
            return
        data = _event_data_to_dict(event.event_data)
        title = _coerce_str(data.get("title"))
        text = _coerce_str(data.get("text"))
        if not title and not text:
            return
        await self._push(title, text, extras=_build_extras(data))

    # ------------------------------------------------------------------ #
    # 极光推送 v3
    # ------------------------------------------------------------------ #
    async def _push(
        self, title: str, text: str, extras: dict | None = None
    ) -> tuple[bool, str]:
        if not self._appkey or not self._mastersecret:
            logger.warning("AppPushMsg: 未配置 JPush AppKey / Master Secret")
            return False, "未配置 JPush 服务端凭据"

        credentials = base64.b64encode(
            f"{self._appkey}:{self._mastersecret}".encode("utf-8")
        ).decode("ascii")
        headers = {"Authorization": f"Basic {credentials}"}
        payload = {
            "platform": ["android", "ios", "hmos"],
            "audience": {"alias": [self._token]},
            "notification": _compose_notification(title, text, extras or {}),
            # apns_production 仅影响 iOS；鸿蒙/Android 走厂商通道不受影响。
            "options": {"apns_production": True},
        }

        try:
            # app.sdk.network.AsyncRequestUtils.post(url, data=None, json=None, **kwargs)
            # raise_exception=True 时网络异常向上抛（httpx2.RequestError 等），
            # HTTP 4xx/5xx 不抛，通过 response.status_code 判定。
            response = await AsyncRequestUtils().post(
                self.JPUSH_PUSH_URL,
                json=payload,
                headers=headers,
                raise_exception=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AppPushMsg 推送请求异常: {exc}")
            return False, f"推送请求异常: {exc}"

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
            logger.info(f"AppPushMsg 推送成功 msg_id={msg_id}")
            return True, f"推送成功，msg_id={msg_id}"

        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code") or status
        message = error.get("message") or "推送失败"
        logger.error(f"AppPushMsg 推送失败: {code} {message}")
        return False, f"推送失败（{code}）：{message}"
