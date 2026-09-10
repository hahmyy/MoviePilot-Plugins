# -*- coding: utf-8 -*-
"""MoviePilot V2 版 AppPushMsg：把 MoviePilot 通知推送到鸿蒙/Android/iOS（系统级推送）。

与 plugins.v3/apppushmsg 功能一致，按 V2 宿主 SDK 实现：
- 事件与 logger 走 app.core.event / app.log；
- HTTP 走 app.utils.http.RequestUtils（requests，同步）；
- 事件处理器为同步方法，由 V2 eventmanager 放入线程池执行。
"""
from __future__ import annotations

import base64
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


def _coerce_str(value: Any) -> str:
    """把任意事件值转成可推送的字符串。"""
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip()

def _mask_token(token: Any) -> str:
    """把目标 Alias 脱敏后用于页面展示。"""
    value = _coerce_str(token)
    if not value:
        return "未配置"
    if len(value) <= 6:
        return value[0] + "***"
    return value[:4] + "***" + value[-2:]

def _now_str() -> str:
    """返回本地时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_stats() -> dict:
    """推送统计的默认结构。"""
    return {
        "push_total": 0,
        "push_success": 0,
        "push_failure": 0,
        "test_calls": 0,
        "notice_total": 0,
        "last_push_time": "",
        "last_push_ok": None,
        "last_push_message": "",
        "last_test_time": "",
        "last_test_code": 0,
        "last_test_message": "",
    }


def _truncate(value: Any, limit: int = 60) -> str:
    """截断长文本，避免仪表盘行过长。"""
    text = _coerce_str(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _append_history(history: Any, entry: dict, limit: int = 20) -> list:
    """把新记录插入历史并只保留最近 limit 条。"""
    items = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    items.insert(0, entry)
    return items[:limit]


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


def _notification_type_options() -> list:
    """枚举宿主消息类型，供配置页多选。"""
    return [{"title": item.value, "value": item.name} for item in NotificationType]


def _notification_type_name(value: Any) -> str:
    """把事件里的消息类型统一成 NotificationType 的成员名。"""
    if value is None:
        return ""
    name = getattr(value, "name", "")
    if name:
        return str(name)
    text = _coerce_str(value)
    for item in NotificationType:
        if text in (item.name, item.value):
            return item.name
    return ""


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
    plugin_version = "0.1.3"
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
    TEST_TITLE = "App 推送测试"
    TEST_TEXT = "这是一条来自 MoviePilot 的测试通知"

    _enabled = False
    _apikey = ""
    _token = ""
    _appkey = ""
    _mastersecret = ""
    _msgtypes = []

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
        self._msgtypes = list(config.get("msgtypes") or [])

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
                    {
                        "component": "VSelect",
                        "props": {
                            "multiple": True,
                            "chips": True,
                            "model": "msgtypes",
                            "label": "消息类型（不选则转发全部）",
                            "items": _notification_type_options(),
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
            "msgtypes": [],
        }

    def get_page(self) -> List[dict]:
        """插件详情页：展示最近一次测试结果。"""
        result = self._read_data("last_test_result")
        if not isinstance(result, dict) or not result:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mt-2",
                        "text": "暂无测试记录，请在配置页点击「发送测试消息」",
                    },
                }
            ]
        success = result.get("code") == 0
        lines = [
            "测试时间：{}".format(result.get("time") or "-"),
            "目标 Token(Alias)：{}".format(result.get("token") or "未配置"),
            "返回结果：{}".format(result.get("msg") or "-"),
        ]
        return [
            {
                "component": "VCard",
                "props": {
                    "variant": "tonal",
                    "color": "success" if success else "error",
                    "class": "mt-2",
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold pb-1"},
                        "text": "最近一次测试结果：{}".format("成功" if success else "失败"),
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "py-2"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 py-1"},
                                "text": line,
                            }
                            for line in lines
                        ],
                    },
                ],
            }
        ]

    # ------------------------------------------------------------------ #
    # 仪表盘：调用次数、连接状态与历史消息
    # ------------------------------------------------------------------ #
    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        """声明仪表盘入口，供宿主仪表盘列表展示。"""
        return [{"key": "apppushmsg_dashboard", "name": "App 推送统计"}]

    def get_dashboard(self, key: str = None, **kwargs):
        """返回插件仪表盘：调用统计、连接状态与最近消息。"""
        stats = self._load_stats()
        history = self._read_data("push_history")
        if not isinstance(history, list):
            history = []
        elements = [
            self._dashboard_status_alert(stats),
            {
                "component": "VRow",
                "content": [
                    self._dashboard_stat_card(
                        "调用次数",
                        str(stats["push_total"]),
                        "mdi-send",
                        "primary",
                        "测试 {} · 通知 {}".format(stats["test_calls"], stats["notice_total"]),
                    ),
                    self._dashboard_stat_card(
                        "成功",
                        str(stats["push_success"]),
                        "mdi-check-circle",
                        "success",
                        "最近 {}".format(stats["last_push_time"] or "-"),
                    ),
                    self._dashboard_stat_card(
                        "失败",
                        str(stats["push_failure"]),
                        "mdi-alert-circle",
                        "error" if stats["push_failure"] else "success",
                        stats["last_push_message"] or "暂无失败记录",
                    ),
                    self._dashboard_stat_card(
                        "最近测试",
                        stats["last_test_time"] or "暂无",
                        "mdi-test-tube",
                        "info",
                        "code {} · {}".format(stats["last_test_code"], stats["last_test_message"] or "-"),
                    ),
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mt-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold pb-1"},
                        "text": "历史消息（最近 {} 条）".format(len(history)),
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "py-2"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 py-1"},
                                "text": line,
                            }
                            for line in self._history_lines(history)
                        ],
                    },
                ],
            },
        ]
        cols = {"cols": 12, "md": 6}
        attrs = {
            "refresh": 30,
            "border": True,
            "title": "App 推送统计",
            "subtitle": "调用次数、连接状态与最近消息",
        }
        return cols, attrs, elements

    def _dashboard_status_alert(self, stats: dict) -> dict:
        configured = bool(self._enabled and self._appkey and self._mastersecret and self._token)
        if not configured:
            alert_type = "warning"
            text = "未就绪：请启用插件并配置极光 AppKey / Master Secret / App Push Token"
        elif stats.get("last_push_ok") is True:
            alert_type = "success"
            text = "连接正常：最近一次推送成功（{}）".format(stats.get("last_push_time") or "-")
        elif stats.get("last_push_ok") is False:
            alert_type = "error"
            text = "连接异常：最近一次推送失败（{}）：{}".format(
                stats.get("last_push_time") or "-",
                stats.get("last_push_message") or "-",
            )
        else:
            alert_type = "info"
            text = "暂无推送记录，连接状态待验证"
        return {
            "component": "VAlert",
            "props": {
                "type": alert_type,
                "variant": "tonal",
                "density": "compact",
                "class": "mb-3",
            },
            "text": text,
        }

    @staticmethod
    def _dashboard_stat_card(label: str, value: str, icon: str, color: str, subtitle: str = "") -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "d-flex align-center ga-3"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "flex-grow-1", "style": "min-width: 0;"},
                                    "content": [
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption text-medium-emphasis text-truncate d-block"},
                                            "text": label,
                                        },
                                        {
                                            "component": "div",
                                            "props": {"class": "text-h6 text-truncate"},
                                            "text": value,
                                        },
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption text-medium-emphasis text-truncate d-block"},
                                            "text": subtitle or "-",
                                        },
                                    ],
                                },
                                {
                                    "component": "VIcon",
                                    "props": {"color": color, "size": "28", "class": "flex-shrink-0"},
                                    "text": icon,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _history_lines(history: list) -> list:
        if not history:
            return ["暂无历史消息"]
        lines = []
        for item in history[:10]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "[{}] {} · {} · {}：{}".format(
                    item.get("time") or "-",
                    item.get("kind") or "推送",
                    "成功" if item.get("ok") else "失败",
                    _truncate(item.get("title") or "-", 20),
                    _truncate(item.get("summary") or item.get("message") or "-", 60),
                )
            )
        return lines or ["暂无历史消息"]

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
        """测试接口：校验 apikey 后发送测试通知，并记录最近一次结果。"""
        result, attempted = self._execute_test(apikey)
        self._record_event("测试", self.TEST_TITLE, self.TEST_TEXT, result, attempted)
        return result

    def _execute_test(self, apikey: str = None):
        if not self._enabled:
            return {"code": 1, "msg": "插件未启用"}, False
        if not self._apikey or (apikey or "").strip() != self._apikey.strip():
            return {"code": 1, "msg": "Push Key 校验失败"}, False
        if not self._token:
            return {"code": 1, "msg": "未配置 App Push Token"}, False
        if not self._appkey or not self._mastersecret:
            return {"code": 1, "msg": "未配置 JPush 服务端凭据"}, False
        ok, message = self._push(self.TEST_TITLE, self.TEST_TEXT)
        return {"code": 0 if ok else 1, "msg": message}, True

    def _read_data(self, key: str):
        """读插件数据，宿主数据层不可用时返回空。"""
        try:
            return self.get_data(key)
        except Exception:  # noqa: BLE001
            return None

    def _write_data(self, key: str, value) -> None:
        """写插件数据，失败只记日志、不影响推送主流程。"""
        try:
            self.save_data(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("AppPushMsg 写入插件数据失败: {}".format(exc))

    def _load_stats(self) -> dict:
        data = _default_stats()
        stored = self._read_data("push_stats")
        if isinstance(stored, dict):
            for key in data:
                if key in stored:
                    data[key] = stored[key]
        return data

    def _record_event(self, kind: str, title: str, text: str, result: dict, attempted: bool) -> None:
        """记录一次测试或通知推送，更新统计与历史。"""
        stats = self._load_stats()
        now = _now_str()
        success = int(result.get("code") or 0) == 0
        if kind == "测试":
            stats["test_calls"] = int(stats.get("test_calls") or 0) + 1
            stats["last_test_time"] = now
            stats["last_test_code"] = int(result.get("code") or 0)
            stats["last_test_message"] = str(result.get("msg") or "")
        else:
            stats["notice_total"] = int(stats.get("notice_total") or 0) + 1
        if attempted:
            stats["push_total"] = int(stats.get("push_total") or 0) + 1
            stats["last_push_time"] = now
            stats["last_push_ok"] = success
            stats["last_push_message"] = str(result.get("msg") or "")
            if success:
                stats["push_success"] = int(stats.get("push_success") or 0) + 1
            else:
                stats["push_failure"] = int(stats.get("push_failure") or 0) + 1
        history = _append_history(
            self._read_data("push_history"),
            {
                "time": now,
                "kind": kind,
                "title": _truncate(title, 40),
                "summary": _truncate(text, 80),
                "ok": success,
                "message": _truncate(result.get("msg"), 120),
            },
        )
        self._write_data("push_stats", stats)
        self._write_data("push_history", history)
        if kind == "测试":
            self._write_data(
                "last_test_result",
                {
                    "time": now,
                    "code": stats["last_test_code"],
                    "msg": stats["last_test_message"],
                    "token": _mask_token(self._token),
                },
            )

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
        type_name = _notification_type_name(data.get("type"))
        if type_name and self._msgtypes and type_name not in self._msgtypes:
            logger.debug("AppPushMsg 消息类型 {} 未开启转发，已跳过".format(type_name))
            return
        ok, message = self._push(title, text, extras=_build_extras(data))
        self._record_event(
            "通知",
            title,
            text,
            {"code": 0 if ok else 1, "msg": message},
            True,
        )

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