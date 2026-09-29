from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from enum import Enum
from typing import Any

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception:  # pragma: no cover - 运行期由 _build_service_account_jwt 报错
    hashes = None
    serialization = None
    padding = None

from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
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


def _build_extras(
    event_data: dict, title: str = "", text: str = "", msgtype: str = ""
) -> dict:
    """构建推送附加字段契约（极光 extras 与华为 clickAction.data 共用）。

    固定字段：page / title / text / msgtype / channel / source / userid / ts；
    type 为兼容旧版别名，值与 msgtype 一致。text 截断到 300 字，避免超出
    华为 4096 字节消息体限制。
    """
    data = event_data if isinstance(event_data, dict) else {}
    raw_title = _coerce_str(title) or _coerce_str(data.get("title"))
    raw_text = _coerce_str(text) or _coerce_str(data.get("text"))
    type_name = (
        _coerce_str(msgtype)
        or _notification_type_name(data.get("type"))
        or _coerce_str(data.get("msgtype"))
    )
    extras: dict[str, Any] = {
        "page": "system-message",
        "title": _truncate(raw_title, MAX_EXTRAS_TITLE_LENGTH),
        "text": _truncate(raw_text, MAX_EXTRAS_TEXT_LENGTH),
        "ts": int(time.time()),
    }
    if type_name:
        extras["msgtype"] = type_name
        extras["type"] = type_name
    for key in ("channel", "source", "userid"):
        value = _coerce_str(data.get(key))
        if value:
            extras[key] = value
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


# 华为 Push Kit v3 直连（服务账号 JWT / PS256）
# 以下常量与请求结构依据华为官方文档核对（2026-09）：
# - 基于服务账号生成鉴权令牌：PS256 JWT，claims aud/iss/iat/exp
# - 推送场景化消息：POST /v3/{projectId}/messages:send，Header push-type: 0
# - 请求体：payload.notification{category,title,body} + target.token + pushOptions
HUAWEI_PUSH_URL_TEMPLATE = "https://push-api.cloud.huawei.com/v3/{project_id}/messages:send"
HUAWEI_DEFAULT_TOKEN_URI = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
HUAWEI_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
HUAWEI_SUCCESS_CODE = "80000000"
MAX_EXTRAS_TITLE_LENGTH = 120
MAX_EXTRAS_TEXT_LENGTH = 300
MAX_HUAWEI_BODY_LENGTH = 1000


# 华为 Push Kit 业务错误码与 HTTP 状态码的可读化说明（依据官方响应参数文档整理）
HUAWEI_ERROR_HINTS = {
    "80100000": "部分 Token 发送成功（单设备推送一般视为失败）：请检查 Push Token 是否属于当前 projectId、是否申请了 push-type 场景权益、Token 是否有效。",
    "80100001": "请求参数部分错误：请检查 projectId、target.token 与请求体字段。",
    "80100003": "消息结构体错误：请检查 payload.notification 的 category/title/body 等字段。",
    "80100004": "消息过期时间非法：请检查 pushOptions.ttl。",
    "80100022": "消息携带图片未通过风控验签：请检查图片地址与图片风控配置。",
    "80200001": "认证错误：请检查服务账号 JSON（key_id/sub_account/private_key）与 Authorization 是否正确。",
    "80200005": "JWT Token 过期：插件会自动刷新后重试；若持续出现请检查服务器时间。",
    "80300002": "当前应用无权限下发推送消息：请在 AGC 开通 Push Kit 以及对应的场景权益。",
    "80300007": "所有 Push Token 都无效：请在 App 重新获取 Push Token，并确认应用属于当前 projectId 的项目。",
    "80300008": "消息体超过 4096 Bytes：请缩短通知标题或正文。",
    "80300010": "Token 数量超过系统默认上限。",
    "80300029": "测试消息流量受限：请降低测试频率后重试。",
    "80300030": "测试消息单次携带的 Token 数量超过上限。",
    "80300036": "JWT 有效期超过 1 天：插件按 1 小时生成，若出现请检查服务账号配置。",
    "80300037": "应用存在违规处罚，暂时无法发送推送消息。",
    "81000001": "华为推送服务内部错误：请稍后重试。",
}

HUAWEI_HTTP_HINTS = {
    400: "请求参数错误，请结合业务错误码排查",
    401: "鉴权失败，请检查 Authorization 与服务账号配置",
    403: "没有访问权限，请检查 AGC 项目与应用配置",
    404: "接口地址或 projectId 不存在，请检查 project_id 配置",
    429: "请求被限流，请降低发送频率后重试",
    500: "华为推送服务内部错误，请稍后重试",
    502: "网关连接异常，请稍后重试",
    503: "服务限流，请降低发送频率后重试",
}


def _huawei_error_detail(code: str, status: int, message: str) -> str:
    """把华为业务错误码转换为可读的中文说明。"""
    detail = _truncate(message or "推送失败", 160)
    if code == "80100000":
        try:
            payload = json.loads(message or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if isinstance(payload, dict):
            success = payload.get("success", "-")
            failure = payload.get("failure", "-")
            illegal = payload.get("illegalTokens")
            reasons = ",".join(sorted(illegal.keys())) if isinstance(illegal, dict) else ""
            detail = "部分 Token 发送成功（成功 {} / 失败 {}{}）".format(
                success, failure, "；原因：" + reasons if reasons else ""
            )
    hint = HUAWEI_ERROR_HINTS.get(code) or HUAWEI_HTTP_HINTS.get(int(status or 0)) or ""
    if hint:
        return "{}；建议：{}".format(detail, hint)
    return detail
HUAWEI_CATEGORIES = [
    "IM",
    "VOIP",
    "MISS_CALL",
    "SUBSCRIPTION",
    "TRAVEL",
    "HEALTH",
    "WORK",
    "ACCOUNT",
    "EXPRESS",
    "FINANCE",
    "DEVICE_REMINDER",
    "MAIL",
    "PLAY_VOICE",
    "MARKETING",
]


def _b64url(data: bytes) -> str:
    """JWT 使用的无填充 Base64URL 编码。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _parse_service_account(raw: Any):
    """解析服务账号 JSON，成功返回 dict，否则返回 None。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None
    return None


def _mask_service_account(account: Any) -> str:
    """服务账号脱敏描述，仅展示非敏感标识。"""
    if not isinstance(account, dict):
        return "未配置"
    sub = _truncate(account.get("sub_account"), 12) or "-"
    key_id = _truncate(account.get("key_id"), 12) or "-"
    return "sub_account={} key_id={}".format(sub, key_id)


def _build_service_account_jwt(account: dict, now: int = None) -> str:
    """按华为官方文档生成服务账号鉴权 JWT（PS256）。"""
    if hashes is None or serialization is None or padding is None:
        raise RuntimeError("缺少 cryptography 依赖，无法生成华为服务账号令牌")
    if not isinstance(account, dict):
        raise ValueError("未配置华为服务账号 JSON")
    key_id = _coerce_str(account.get("key_id"))
    sub_account = _coerce_str(account.get("sub_account"))
    private_key = account.get("private_key")
    if not key_id or not sub_account or not private_key:
        raise ValueError("服务账号 JSON 缺少 key_id / sub_account / private_key")
    issued_at = int(now if now is not None else time.time())
    token_uri = _coerce_str(account.get("token_uri")) or HUAWEI_DEFAULT_TOKEN_URI
    header = {"kid": key_id, "typ": "JWT", "alg": "PS256"}
    claims = {
        "aud": token_uri,
        "iss": sub_account,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    signing_input = "{}.{}".format(
        _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    )
    key = serialization.load_pem_private_key(str(private_key).encode("utf-8"), password=None)
    signature = key.sign(
        signing_input.encode("ascii"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return "{}.{}".format(signing_input, _b64url(signature))


# 前端 VFileInput 上传回调：把文件内容读入 service_account_json 文本字段，
# 模型里不保存 File 对象（FormRender 会以 with(model) 作用域执行该表达式）。
_SERVICE_ACCOUNT_UPLOAD_HANDLER = (
    "(files) => { const f = Array.isArray(files) ? files[0] : files; "
    "if (!f) { return; } const reader = new FileReader(); "
    "reader.onload = function () { service_account_json = String(reader.result || ''); }; "
    "reader.readAsText(f, 'utf-8'); }"
)


def _huawei_category_options() -> list:
    """华为通知分类选项（官方文档取值）。"""
    return [{"title": item, "value": item} for item in HUAWEI_CATEGORIES]


def _build_huawei_payload(
    title: str, text: str, category: str, push_token: str, data: dict = None
) -> dict:
    """按官方 v3 场景化消息结构拼装通知体；点击数据放 clickAction.data。"""
    title = _coerce_str(title) or "MoviePilot"
    body = _coerce_str(text) or title
    category = _coerce_str(category) if _coerce_str(category) in HUAWEI_CATEGORIES else "MARKETING"
    extras = data if isinstance(data, dict) else {}
    return {
        "payload": {
            "notification": {
                "category": category,
                "title": title,
                "body": body,
                "clickAction": {"actionType": 0, "data": extras},
            }
        },
        "target": {"token": [push_token]},
        "pushOptions": {"testMessage": False, "ttl": 86400},
    }


def _parse_huawei_response(status: int, body: Any) -> tuple:
    """解析华为响应，返回 (是否成功, 结果描述)。"""
    body = body if isinstance(body, dict) else {}
    code = _coerce_str(body.get("code")) or str(status or "")
    message = _coerce_str(body.get("msg")) or _coerce_str(body.get("message")) or "推送失败"
    request_id = _coerce_str(body.get("requestId"))
    if int(status or 0) < 400 and code == HUAWEI_SUCCESS_CODE:
        return True, "推送成功，requestId={}".format(request_id or "-")
    detail = _huawei_error_detail(code, status, message)
    text = "推送失败（{}）：{}".format(code or status or "-", detail)
    if request_id:
        text += "（requestId={}）".format(request_id)
    return False, text


def _compose_notification(title: str, text: str, extras: dict) -> dict:
    """按极光 v3 结构拼装多平台通知体，保证 extras 在通用与各平台节点都下发。"""
    title = _coerce_str(title)
    text = _coerce_str(text)
    extras = extras if isinstance(extras, dict) else {}
    alert = text or title
    android = {"alert": alert, "extras": extras}
    hmos = {"alert": alert, "category": "IM", "extras": extras}
    if title:
        android["title"] = title
        hmos["title"] = title
    return {
        "alert": alert,
        "extras": extras,
        "android": android,
        "ios": {
            "alert": "{}\n{}".format(title, text) if title and text else alert,
            "extras": extras,
        },
        "hmos": hmos,
    }


class MoviePilotAppPush(_PluginBase):
    """把 MoviePilot 通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。

    设计约定：
    - 插件 ID 必须为 MoviePilotAppPush（App 端据此渲染专用配置页并调用 /run 测试接口）。
    - App Push Token 即极光 Alias，App 端点“应用”后写入设备。
    - 服务端凭据 appkey / mastersecret 只在服务端配置，不下发、不打印。
    """

    plugin_name = "App 推送"
    plugin_desc = "将 MoviePilot 通知推送到鸿蒙/Android/iOS 客户端（系统级推送）。"
    plugin_icon = "MoviePilotAppPush.png"
    plugin_version = "1.0.12"
    plugin_author = "hahmyy"
    author_url = "https://github.com/hahmyy"
    plugin_config_prefix = "moviepilotapppush_"
    plugin_order = 50
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
    _onlyonce = False
    _test_title = ""
    _test_text = ""
    _channel = "jpush"
    _appid = ""
    _project_id = ""
    _hw_category = "MARKETING"
    _hw_service_account = None
    _hw_token = ""
    _hw_token_mode = ""
    _hw_token_key = ""
    _hw_token_expire_at = 0.0

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
        self._msgtypes = list(config.get("msgtypes") or [])
        self._onlyonce = bool(config.get("onlyonce"))
        self._test_title = str(config.get("testtitle") or "").strip()
        self._test_text = str(config.get("testtext") or "").strip()
        self._channel = str(config.get("channel") or "jpush").strip().lower()
        if self._channel not in ("jpush", "huawei"):
            self._channel = "jpush"
        self._appid = str(config.get("appid") or "").strip()
        self._project_id = str(config.get("project_id") or "").strip()
        self._hw_category = str(config.get("huawei_category") or "MARKETING").strip() or "MARKETING"
        sanitize_account = False
        raw_account = config.get("service_account_json")
        if isinstance(raw_account, str) and raw_account.strip():
            account = _parse_service_account(raw_account)
            if account:
                self._hw_service_account = account
                self._store_service_account(account)
                sanitize_account = True
            else:
                logger.error("MoviePilotAppPush 华为服务账号 JSON 解析失败（内容不记录）")
                self._hw_service_account = None
        else:
            stored = self._read_data("huawei_service_account")
            self._hw_service_account = stored if isinstance(stored, dict) else None
        self._hw_token = ""
        self._hw_token_mode = ""
        self._hw_token_key = ""
        self._hw_token_expire_at = 0.0
        if sanitize_account:
            try:
                self.update_config(self._current_config())
            except Exception as exc:  # noqa: BLE001
                logger.debug("MoviePilotAppPush 清洗服务账号配置失败: {}".format(exc))
        if self._onlyonce:
            self._onlyonce = False
            try:
                self.update_config(self._current_config())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"MoviePilotAppPush 重置测试开关失败: {exc}")
            self._send_test_now()

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

    def _current_config(self) -> dict:
        """导出当前配置，供保存与重置测试开关使用。"""
        return {
            "enabled": self._enabled,
            "apikey": self._apikey,
            "token": self._token,
            "appkey": self._appkey,
            "mastersecret": self._mastersecret,
            "msgtypes": list(self._msgtypes or []),
            "testtitle": self._test_title,
            "testtext": self._test_text,
            "onlyonce": self._onlyonce,
            "channel": self._channel,
            "appid": self._appid,
            "project_id": self._project_id,
            "service_account_json": "",
            "huawei_category": self._hw_category,
        }

    async def _send_test_async(self) -> None:
        """按自定义内容立即发送一条测试通知。"""
        title = self._test_title or self.TEST_TITLE
        text = self._test_text or self.TEST_TEXT
        extras = _build_extras({}, title, text)
        ok, message = await self._push(title, text, extras=extras)
        await self._record_event(
            "测试", title, text, {"code": 0 if ok else 1, "msg": message}, True
        )

    def _send_test_now(self) -> None:
        """在同步的 init_plugin 中执行或调度一次测试发送。"""
        coro = self._send_test_async()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """配置页：按推送渠道即时显示对应字段。"""
        head = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mb-2",
                    "text": "切换推送渠道后会立即显示对应字段：极光填 AppKey / Master Secret，华为填项目 ID 与服务账号 JSON。",
                },
            },
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
            {
                "component": "VSelect",
                "props": {
                    "model": "channel",
                    "label": "推送渠道",
                    "items": [
                        {"title": "极光 JPush", "value": "jpush"},
                        {"title": "华为 Push Kit 直连", "value": "huawei"},
                    ],
                },
            },
            {"component": "VTextField", "props": {"model": "apikey", "label": "Push Key"}},
            {
                "component": "VTextField",
                "props": {
                    "model": "token",
                    "label": "App Push Token（jpush=极光 Alias / huawei=华为 Push Token）",
                },
            },
        ]
        jpush_fields = [
            {
                "component": "VTextField",
                "props": {
                    "model": "appkey",
                    "label": "JPush AppKey",
                    "v-show": "channel !== 'huawei'",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "mastersecret",
                    "label": "JPush Master Secret",
                    "type": "password",
                    "v-show": "channel !== 'huawei'",
                },
            },
        ]
        huawei_fields = [
            {
                "component": "VTextField",
                "props": {
                    "model": "appid",
                    "label": "华为 Client ID（appid，v3 备用）",
                    "v-show": "channel === 'huawei'",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "project_id",
                    "label": "华为项目 ID（projectId）",
                    "v-show": "channel === 'huawei'",
                },
            },
            {
                "component": "VFileInput",
                "props": {
                    "label": "上传服务账号 JSON（自动读取内容）",
                    "accept": ".json,application/json",
                    "prepend-icon": "mdi-upload",
                    "v-show": "channel === 'huawei'",
                    "onUpdate:modelValue": _SERVICE_ACCOUNT_UPLOAD_HANDLER,
                },
            },
            {
                "component": "VTextarea",
                "props": {
                    "model": "service_account_json",
                    "label": "华为服务账号 JSON（可粘贴或上传，留空表示不修改）",
                    "rows": 4,
                    "auto-grow": True,
                    "v-show": "channel === 'huawei'",
                },
            },
            {
                "component": "VSelect",
                "props": {
                    "model": "huawei_category",
                    "label": "华为通知分类",
                    "items": _huawei_category_options(),
                    "v-show": "channel === 'huawei'",
                },
            },
        ]
        tail = [
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
            {
                "component": "VTextField",
                "props": {"model": "testtitle", "label": "测试标题（留空用默认）"},
            },
            {
                "component": "VTextarea",
                "props": {
                    "model": "testtext",
                    "label": "测试内容（留空用默认）",
                    "rows": 2,
                    "auto-grow": True,
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                    "text": "测试已移到插件详情页：点击「发送测试推送」按钮即可测试，不需要保存后再测。",
                },
            },
        ]
        return [{"component": "VForm", "content": head + jpush_fields + huawei_fields + tail}], {
            "enabled": False,
            "channel": "jpush",
            "apikey": "",
            "token": "",
            "appkey": "",
            "mastersecret": "",
            "appid": "",
            "project_id": "",
            "service_account_json": "",
            "huawei_category": "MARKETING",
            "msgtypes": [],
            "testtitle": "",
            "testtext": "",
            "onlyonce": False,
        }

    def get_page(self) -> list[dict]:
        """插件详情页：一键测试、成功率/失败率环形仪表与最近消息。"""
        return (
            self._build_last_test_card()
            + [self._build_test_action_card()]
            + self._build_dashboard_elements()
        )

    def _build_test_action_card(self) -> dict:
        """详情页一键测试卡片：直接调用固定 /run 接口，无需先保存。"""
        if not self._apikey:
            return {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "class": "mt-2",
                    "text": "请先在配置页保存 Push Key 与推送凭据，再回到本页点击测试。",
                },
            }
        ready, ready_message = self._channel_ready()
        if not ready:
            return {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "class": "mt-2",
                    "text": "暂不可测试：{}".format(ready_message),
                },
            }
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mt-2"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1 font-weight-bold pb-1"},
                    "text": "一键测试（无需保存）",
                },
                {
                    "component": "VCardText",
                    "props": {"class": "py-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-body-2 mb-3"},
                            "text": "点击按钮立即调用 /api/v1/plugin/MoviePilotAppPush/run，使用配置页已保存的测试标题与内容，完成后本页统计会自动刷新。",
                        },
                        {
                            "component": "VBtn",
                            "props": {
                                "color": "primary",
                                "variant": "tonal",
                                "prepend-icon": "mdi-send",
                                "block": True,
                            },
                            "text": "发送测试推送",
                            "events": {
                                "click": {
                                    "api": "/plugin/MoviePilotAppPush/run",
                                    "method": "GET",
                                    "params": {
                                        "apikey": self._apikey,
                                        "title": self._test_title or self.TEST_TITLE,
                                        "text": self._test_text or self.TEST_TEXT,
                                    },
                                }
                            },
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _rate_values(stats: dict) -> tuple:
        """计算成功率、失败率（百分比）与总尝试次数。"""
        success = int(stats.get("push_success") or 0)
        failure = int(stats.get("push_failure") or 0)
        attempts = success + failure
        if attempts <= 0:
            return 0.0, 0.0, 0
        return success * 100.0 / attempts, failure * 100.0 / attempts, attempts

    @staticmethod
    def _rate_ring(title: str, rate: float, color: str, subtitle: str) -> dict:
        """构建环形仪表盘卡片（VProgressCircular）。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "class": "h-100"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "d-flex flex-column align-center justify-center py-4"},
                            "content": [
                                {
                                    "component": "VProgressCircular",
                                    "props": {
                                        "model-value": round(max(0.0, min(100.0, rate)), 1),
                                        "size": 120,
                                        "width": 12,
                                        "color": color,
                                    },
                                    "text": "{:.0f}%".format(rate),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-subtitle-2 mt-3"},
                                    "text": title,
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-medium-emphasis"},
                                    "text": subtitle,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    def _build_last_test_card(self) -> list[dict]:
        """构建最近一次测试结果卡片。"""
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
    def _build_dashboard_elements(self) -> list[dict]:
        """构建仪表盘与详情页共用的统计元素。"""
        stats = self._load_stats()
        history = self._read_data("push_history")
        if not isinstance(history, list):
            history = []
        success_rate, failure_rate, attempts = self._rate_values(stats)
        return [
            self._dashboard_status_alert(stats),
            {
                "component": "VRow",
                "props": {"class": "mt-1"},
                "content": [
                    self._rate_ring(
                        "成功率",
                        success_rate,
                        "success",
                        "成功 {} / 共 {} 次".format(stats.get("push_success", 0), attempts)
                        if attempts else "暂无推送记录",
                    ),
                    self._rate_ring(
                        "失败率",
                        failure_rate,
                        "error",
                        "失败 {} / 共 {} 次".format(stats.get("push_failure", 0), attempts)
                        if attempts else "暂无推送记录",
                    ),
                ],
            },
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

    def get_dashboard_meta(self) -> list[dict[str, str]]:
        """声明仪表盘入口，供宿主仪表盘列表展示。"""
        return [{"key": "moviepilotapppush_dashboard", "name": "App 推送统计"}]

    def get_dashboard(self, key: str | None = None, **kwargs):
        """返回插件仪表盘：调用统计、连接状态与最近消息。"""
        elements = self._build_dashboard_elements()
        cols = {"cols": 12, "md": 6}
        attrs = {
            "refresh": 30,
            "border": True,
            "title": "App 推送统计",
            "subtitle": "调用次数、连接状态与最近消息",
        }
        return cols, attrs, elements

    def _dashboard_status_alert(self, stats: dict) -> dict:
        ready, ready_message = self._channel_ready()
        configured = bool(self._enabled and ready)
        channel_name = "华为 Push Kit" if self._channel == "huawei" else "极光 JPush"
        if not self._enabled:
            alert_type = "warning"
            text = "未就绪：插件未启用"
        elif not configured:
            alert_type = "warning"
            text = "未就绪（{}）：{}".format(channel_name, ready_message)
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
                "[{}] {} · {} · {} · {}：{}".format(
                    item.get("time") or "-",
                    "华为" if item.get("channel") == "huawei" else "极光",
                    item.get("kind") or "推送",
                    "成功" if item.get("ok") else "失败",
                    _truncate(item.get("title") or "-", 20),
                    _truncate(item.get("summary") or item.get("message") or "-", 60),
                )
            )
        return lines or ["暂无历史消息"]

    # ------------------------------------------------------------------ #
    # API：App 配置页“测试”按钮调用
    # 最终路径：/api/v1/plugin/MoviePilotAppPush/run
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

    async def run(
        self, apikey: str | None = None, title: str | None = None, text: str | None = None
    ) -> dict[str, Any]:
        """测试接口：校验 apikey 后发送测试通知，并记录最近一次结果。"""
        send_title = _coerce_str(title) or self._test_title or self.TEST_TITLE
        send_text = _coerce_str(text) or self._test_text or self.TEST_TEXT
        result, attempted = await self._execute_test(apikey, title, text)
        await self._record_event("测试", send_title, send_text, result, attempted)
        return result

    def _channel_ready(self) -> tuple[bool, str]:
        """检查当前渠道的最小可用配置。"""
        if self._channel == "huawei":
            if not self._token:
                return False, "未配置华为 Push Token"
            account = self._hw_service_account
            project_id = self._project_id or (
                _coerce_str(account.get("project_id")) if isinstance(account, dict) else ""
            )
            if not project_id:
                return False, "未配置华为项目 ID（projectId）"
            if not isinstance(account, dict):
                return False, "未配置华为服务账号 JSON"
            return True, ""
        if not self._token:
            return False, "未配置 App Push Token"
        if not self._appkey or not self._mastersecret:
            return False, "未配置 JPush 服务端凭据"
        return True, ""

    async def _execute_test(
        self, apikey: str | None = None, title: str | None = None, text: str | None = None
    ):
        if not self._enabled:
            return {"code": 1, "msg": "插件未启用"}, False
        if not self._apikey or (apikey or "").strip() != self._apikey.strip():
            return {"code": 1, "msg": "Push Key 校验失败"}, False
        ready, ready_message = self._channel_ready()
        if not ready:
            return {"code": 1, "msg": ready_message}, False
        send_title = _coerce_str(title) or self._test_title or self.TEST_TITLE
        send_text = _coerce_str(text) or self._test_text or self.TEST_TEXT
        ok, message = await self._push(
            send_title, send_text, extras=_build_extras({}, send_title, send_text)
        )
        return {"code": 0 if ok else 1, "msg": message}, True


    def _read_data(self, key: str):
        """读插件数据，宿主数据层不可用时返回空。"""
        try:
            return self.get_data(key)
        except Exception:  # noqa: BLE001
            return None

    async def _write_data(self, key: str, value) -> None:
        """写插件数据，失败只记日志、不影响推送主流程。"""
        try:
            await self.async_save_data(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"MoviePilotAppPush 写入插件数据失败: {exc}")

    def _store_service_account(self, account: dict) -> None:
        """服务账号 JSON 只落到插件数据，避免出现在配置响应中。"""
        try:
            self.save_data("huawei_service_account", account)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"MoviePilotAppPush 保存华为服务账号失败: {exc}")

    def _load_stats(self) -> dict:
        data = _default_stats()
        stored = self._read_data("push_stats")
        if isinstance(stored, dict):
            for key in data:
                if key in stored:
                    data[key] = stored[key]
        return data

    async def _record_event(self, kind: str, title: str, text: str, result: dict, attempted: bool) -> None:
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
                "channel": self._channel,
                "title": _truncate(title, 40),
                "summary": _truncate(text, 80),
                "ok": success,
                "message": _truncate(result.get("msg"), 120),
            },
        )
        await self._write_data("push_stats", stats)
        await self._write_data("push_history", history)
        if kind == "测试":
            await self._write_data(
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
        type_name = _notification_type_name(data.get("type"))
        if type_name and self._msgtypes and type_name not in self._msgtypes:
            logger.debug("MoviePilotAppPush 消息类型 {} 未开启转发，已跳过".format(type_name))
            return
        ok, message = await self._push(title, text, extras=_build_extras(data, title, text))
        await self._record_event(
            "通知",
            title,
            text,
            {"code": 0 if ok else 1, "msg": message},
            True,
        )

    # ------------------------------------------------------------------ #
    # 极光推送 v3
    # ------------------------------------------------------------------ #
    async def _push(
        self, title: str, text: str, extras: dict | None = None
    ) -> tuple[bool, str]:
        """按配置渠道分发推送。"""
        if self._channel == "huawei":
            return await self._push_huawei(title, text, extras)
        return await self._push_jpush(title, text, extras)

    async def _push_huawei(self, title: str, text: str, extras: dict | None = None) -> tuple[bool, str]:
        """华为 Push Kit v3 直连推送。"""
        if not self._token:
            return False, "未配置华为 Push Token"
        account = self._hw_service_account
        project_id = self._project_id or (
            _coerce_str(account.get("project_id")) if isinstance(account, dict) else ""
        )
        if not project_id:
            return False, "未配置华为项目 ID（projectId）"
        if not isinstance(account, dict):
            return False, "未配置华为服务账号 JSON"
        try:
            token, mode = await self._get_huawei_token()
        except Exception as exc:  # noqa: BLE001
            logger.error("MoviePilotAppPush 华为鉴权失败: {}".format(exc))
            return False, "华为鉴权失败: {}".format(exc)
        url = HUAWEI_PUSH_URL_TEMPLATE.format(project_id=project_id)
        extras = extras if isinstance(extras, dict) and extras else _build_extras({}, title, text)
        payload = _build_huawei_payload(
            _truncate(title, MAX_EXTRAS_TITLE_LENGTH) or title,
            _truncate(text, MAX_HUAWEI_BODY_LENGTH) or text,
            self._hw_category,
            self._token,
            extras,
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(token),
            "push-type": "0",
        }
        try:
            response = await AsyncRequestUtils().post(
                url, json=payload, headers=headers, raise_exception=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("MoviePilotAppPush 华为推送请求异常: {}".format(exc))
            return False, "推送请求异常: {}".format(exc)
        if response is None:
            return False, "推送网关无响应"
        status = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        ok, message = _parse_huawei_response(status, body)
        if ok:
            logger.info("MoviePilotAppPush 华为推送成功（{}）: {}".format(mode, message))
        else:
            logger.error("MoviePilotAppPush 华为推送失败: {}".format(message))
        return ok, message

    async def _get_huawei_token(self) -> tuple[str, str]:
        """获取华为鉴权令牌：优先 JWT 换 access_token，失败回退官方 JWT 直连。"""
        account = self._hw_service_account
        if not isinstance(account, dict):
            raise ValueError("未配置华为服务账号 JSON")
        cache_key = "{}|{}|{}".format(
            _coerce_str(account.get("key_id")),
            _coerce_str(account.get("sub_account")),
            self._project_id,
        )
        now = time.time()
        if (
            self._hw_token
            and self._hw_token_key == cache_key
            and now < float(self._hw_token_expire_at or 0)
        ):
            return self._hw_token, self._hw_token_mode
        assertion = _build_service_account_jwt(account)
        exchanged = await self._exchange_huawei_access_token(account, assertion)
        if exchanged:
            mode = "access_token"
            value, expires_in = exchanged
            ttl = max(60, int(expires_in or 3600) - 60)
        else:
            mode = "jwt"
            value, ttl = assertion, 3300
        self._hw_token = value
        self._hw_token_mode = mode
        self._hw_token_key = cache_key
        self._hw_token_expire_at = now + ttl
        logger.info("MoviePilotAppPush 华为鉴权令牌已刷新（{}）".format(mode))
        return value, mode

    async def _exchange_huawei_access_token(self, account: dict, assertion: str):
        """用服务账号 JWT 换取 access_token；不可用时返回 None（回退 JWT 直连）。"""
        token_uri = _coerce_str(account.get("token_uri")) or HUAWEI_DEFAULT_TOKEN_URI
        try:
            response = await AsyncRequestUtils().post(
                token_uri,
                data={"grant_type": HUAWEI_JWT_BEARER_GRANT, "assertion": assertion},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                raise_exception=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("MoviePilotAppPush 华为 access_token 换取失败，回退 JWT 直连: {}".format(exc))
            return None
        if response is None:
            return None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(body, dict):
            return None
        access_token = _coerce_str(body.get("access_token"))
        if not access_token:
            return None
        return access_token, body.get("expires_in") or 3300

    async def _push_jpush(
        self, title: str, text: str, extras: dict | None = None
    ) -> tuple[bool, str]:
        if not self._appkey or not self._mastersecret:
            logger.warning("MoviePilotAppPush: 未配置 JPush AppKey / Master Secret")
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
            logger.error(f"MoviePilotAppPush 推送请求异常: {exc}")
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
            logger.info(f"MoviePilotAppPush 推送成功 msg_id={msg_id}")
            return True, f"推送成功，msg_id={msg_id}"

        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code") or status
        message = error.get("message") or "推送失败"
        logger.error(f"MoviePilotAppPush 推送失败: {code} {message}")
        return False, f"推送失败（{code}）：{message}"
