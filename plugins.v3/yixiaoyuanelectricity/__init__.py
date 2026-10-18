"""易校园寝室电费/电量监控（MoviePilot V3 插件）。

做三件事：
1. 用手机号 + 短信验证码登录易校园，拿到 UID（之后免登录续期）。
2. 起一条企业微信「智能机器人」长连接，群里 @ 一下就能查余额、改阈值。
3. 定时查余额，低于阈值主动推到群里（markdown）。

实现只用标准库：AES 用于复刻 App 的 appSecurityToken，WebSocket 客户端手写，
不引入任何第三方依赖，也不写插件源码目录。
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import os
import random
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import Request

try:  # V3 稳定入口
    from app.sdk.plugin import _PluginBase
except Exception:  # noqa: BLE001 - 兼容旧宿主
    from app.plugins import _PluginBase

try:
    from app.sdk.logging import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger(__name__)


# ======================================================================
# 易校园接口常量（从 App 7.8.7 逆出来的，见仓库 README）
# ======================================================================
BASE_AUTH = "https://auth.xiaofubao.com"
BASE_APP = "https://application.xiaofubao.com"
BASE_CAMPUS = "https://compus.xiaofubao.com"

APP_ID = "1810181825222034"
CLIENT_ID = "65l09sfwa9ao2dc"
APP_VER = "787"
APP_VER_NAME = "7.8.7"
PLATFORM = "YUNMA_APP"
OS_TYPE = "iOS"
OS_VERSION = "15.6"
SESSION_KEY = "shiroJID"

UA_APP = (
    f"{OS_TYPE}/{OS_VERSION}/iPad8,6/WKWebview ZJYXYwebviewbroswer "
    f"ZJYXYIphone tourCustomer /yunmaapp.NET/{APP_VER_NAME}/"
)
UA_WEB = (
    "Mozilla/5.0 (Linux; Android 11; Android for arm64; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/66.0.3359.158 Mobile Safari/537.36 "
    "ZJYXYwebviewbroswer ZJYXYAndroid tourCustomer/yunmaapp.NET/" + APP_VER_NAME + "/"
)

WS_URL = "wss://openws.work.weixin.qq.com"
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "ping"
CMD_RESPOND = "aibot_respond_msg"
CMD_CALLBACK = "aibot_msg_callback"
CMD_EVENT = "aibot_event_callback"
CMD_SEND = "aibot_send_msg"


# ======================================================================
# AES-128-ECB（纯标准库，只为生成 appSecurityToken）
# ======================================================================
def _build_tables():
    def xtime(a: int) -> int:
        a <<= 1
        return (a ^ 0x1B) & 0xFF if a & 0x100 else a

    def mul(a: int, b: int) -> int:
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            a = xtime(a)
            b >>= 1
        return p

    def rotl8(x: int, s: int) -> int:
        return ((x << s) | (x >> (8 - s))) & 0xFF

    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if mul(a, b) == 1:
                inv[a] = b
                break

    sbox = [0] * 256
    for i in range(256):
        x = inv[i]
        sbox[i] = x ^ rotl8(x, 1) ^ rotl8(x, 2) ^ rotl8(x, 3) ^ rotl8(x, 4) ^ 0x63
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox, mul, xtime


SBOX, INV_SBOX, _GMUL, _XTIME = _build_tables()


def _expand_key(key: bytes) -> list[bytes]:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    nk, nr = 4, 10
    w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _XTIME(rcon)
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [bytes(sum(w[4 * r:4 * r + 4], [])) for r in range(nr + 1)]


def _ark(s, k):
    return [s[i] ^ k[i] for i in range(16)]


def _shift_rows(s):
    return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]


def _inv_shift_rows(s):
    return [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _GMUL(a[0], 2) ^ _GMUL(a[1], 3) ^ a[2] ^ a[3]
        out[c * 4 + 1] = a[0] ^ _GMUL(a[1], 2) ^ _GMUL(a[2], 3) ^ a[3]
        out[c * 4 + 2] = a[0] ^ a[1] ^ _GMUL(a[2], 2) ^ _GMUL(a[3], 3)
        out[c * 4 + 3] = _GMUL(a[0], 3) ^ a[1] ^ a[2] ^ _GMUL(a[3], 2)
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _GMUL(a[0], 14) ^ _GMUL(a[1], 11) ^ _GMUL(a[2], 13) ^ _GMUL(a[3], 9)
        out[c * 4 + 1] = _GMUL(a[0], 9) ^ _GMUL(a[1], 14) ^ _GMUL(a[2], 11) ^ _GMUL(a[3], 13)
        out[c * 4 + 2] = _GMUL(a[0], 13) ^ _GMUL(a[1], 9) ^ _GMUL(a[2], 14) ^ _GMUL(a[3], 11)
        out[c * 4 + 3] = _GMUL(a[0], 11) ^ _GMUL(a[1], 13) ^ _GMUL(a[2], 9) ^ _GMUL(a[3], 14)
    return out


def _enc_block(block: bytes, rk) -> bytes:
    s = _ark(list(block), rk[0])
    for r in range(1, 10):
        s = [SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        s = _ark(s, rk[r])
    s = [SBOX[b] for b in s]
    s = _shift_rows(s)
    return bytes(_ark(s, rk[10]))


def _dec_block(block: bytes, rk) -> bytes:
    s = _ark(list(block), rk[10])
    for r in range(9, 0, -1):
        s = _inv_shift_rows(s)
        s = [INV_SBOX[b] for b in s]
        s = _ark(s, rk[r])
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [INV_SBOX[b] for b in s]
    return bytes(_ark(s, rk[0]))


def _pkcs7_pad(data: bytes, n: int = 16) -> bytes:
    k = n - len(data) % n
    return data + bytes([k]) * k


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    k = data[-1]
    if 1 <= k <= 16 and data[-k:] == bytes([k]) * k:
        return data[:-k]
    return data


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    data = _pkcs7_pad(data)
    return b"".join(_enc_block(data[i:i + 16], rk) for i in range(0, len(data), 16))


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    out = b"".join(_dec_block(data[i:i + 16], rk) for i in range(0, len(data), 16))
    return _pkcs7_unpad(out)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def gen_device_id() -> str:
    return "yunma" + "".join(random.choice("0123456789abcdef") for _ in range(32))


def app_security_token(security_token: str, device_id: str) -> str:
    """复刻 App：AES 解出服务端下发的内容，再拼 md5 链加密回去。"""
    key = security_token[:16].encode("ascii")
    payload = base64.b64decode(security_token[32:])
    text = aes_ecb_decrypt(key, payload).decode("utf-8", "ignore")
    ts = str(int(time.time()))
    stage1 = md5_hex(f"{device_id}|{PLATFORM}|{text}|{ts}|{APP_VER_NAME}").upper()
    stage2 = md5_hex(stage1).upper()
    stage3 = f"{device_id}|{PLATFORM}|{text}|{ts}|{APP_VER_NAME}|{stage2}"
    return base64.b64encode(aes_ecb_encrypt(key, stage3.encode("utf-8"))).decode("ascii")


# ======================================================================
# HTTP
# ======================================================================
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class HttpError(RuntimeError):
    pass


def _request(method: str, url: str, form: dict | None = None,
             headers: dict | None = None, timeout: int = 25) -> tuple[int, dict, bytes]:
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    base = {"User-Agent": UA_APP}
    if form is not None:
        base["Content-Type"] = "application/x-www-form-urlencoded"
    base.update(headers or {})
    for k, v in base.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPSHandler(context=SSL_CTX)
)


def _request_no_redirect(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=25) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise HttpError(f"响应不是 JSON：{raw[:160]!r}")


# ======================================================================
# 易校园业务
# ======================================================================
def campus_form(device_id: str) -> dict:
    return {"appVersion": APP_VER, "deviceId": device_id, "platform": PLATFORM, "testAccount": "1"}


def get_security_info(device_id: str) -> dict:
    """取 securityToken 与风控等级。

    `level == 0` 表示可以直接发短信；`level != 0` 表示易校园要求先过图形验证码
    （通常是短时间发太多次短信触发的风控）。
    """
    _, _, raw = _request("POST", BASE_CAMPUS + "/common/security/token", campus_form(device_id))
    info = _json(raw)
    if not info.get("success"):
        raise HttpError(f"取 securityToken 失败：{info.get('message')}")
    data = info.get("data") or {}
    return {"level": int(data.get("level") or 0), "securityToken": data.get("securityToken") or ""}


def get_captcha_png(device_id: str, security_token: str) -> bytes:
    """取图形验证码图片（PNG 字节）。"""
    body = campus_form(device_id)
    body["securityToken"] = security_token
    _, _, raw = _request("POST", BASE_CAMPUS + "/common/security/imageCaptcha", body)
    res = _json(raw)
    if not res.get("success"):
        raise HttpError(f"取图形验证码失败：{res.get('message')}")
    data = res.get("data")
    if not isinstance(data, str) or not data.strip():
        raise HttpError("图形验证码为空")
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        return base64.b64decode(data)
    except Exception as exc:  # noqa: BLE001
        raise HttpError(f"图形验证码解码失败：{exc}")


def send_login_code(phone: str, device_id: str, security_token: str, captcha: str = "") -> dict:
    """发短信验证码；需要图形验证码时把 captcha 一起带上。"""
    body = campus_form(device_id)
    body.update({
        "appSecurityToken": app_security_token(security_token, device_id),
        "securityToken": security_token,
        "sendCount": "1",
        "mobilePhone": phone,
    })
    if captcha:
        body["imageCaptchaValue"] = captcha
    _, _, raw = _request("POST", BASE_CAMPUS + "/compus/user/sendLoginVerificationCode", body)
    res = _json(raw)
    if not res.get("success"):
        raise HttpError(f"{res.get('message')}（bizCode {res.get('bizCode')}）")
    return {"userExists": (res.get("data") or {}).get("userExists")}


def verify_login(phone: str, device_id: str, code: str) -> dict:
    """验证码登录，返回 UID/学校等信息。"""
    body = campus_form(device_id)
    body.update({
        "clientId": CLIENT_ID,
        "mobilePhone": phone,
        "osType": OS_TYPE,
        "osUuid": device_id,
        "osVersion": OS_VERSION,
        "verificationCode": code,
    })
    _, _, raw = _request("POST", BASE_CAMPUS + "/login/doLoginByVerificationCode", body)
    res = _json(raw)
    if not res.get("success"):
        raise HttpError(f"登录失败：{res.get('message')}（{res.get('bizCode')}）")
    return res.get("data") or {}


def oauth_authorize(uid: str) -> tuple[str, dict]:
    """UID -> shiroJID 会话，不需要密码，可反复刷新。"""
    query = urllib.parse.urlencode({
        "bindSkip": "1",
        "authType": "2",
        "appid": APP_ID,
        "callbackUrl": BASE_APP + "/",
        "unionid": str(uid),
    })
    status, headers, raw = _request_no_redirect(
        BASE_AUTH + "/authoriz/getCodeV2?" + query,
        {"User-Agent": UA_WEB, "X-Requested-With": "cn.com.yunma.school.app"},
    )
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise HttpError("UID 无效或账号不存在（OAuth 被拒绝）")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = (params.get("ymCode") or params.get("code") or [None])[0]
    if not code:
        raise HttpError(f"OAuth 跳转里没有 code：{location}")

    # 关键：这一步会连发 3 条 Set-Cookie，形如
    #   shiroJID=<新的>; shiroJID=deleteMe(Max-Age=0); shiroJID=<最终可用的>
    # 用 dict(resp.headers) 只能拿到第一条，那条随后就被 deleteMe 作废了，
    # 后面查电量必然 204。必须用 CookieJar，让它按顺序覆盖，最后留下真正有效的那条。
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=SSL_CTX),
    )
    body = urllib.parse.urlencode({"code": code}).encode()
    request = urllib.request.Request(
        BASE_APP + "/app/login/getUser4Authorize", data=body, method="POST")
    request.add_header("User-Agent", UA_APP)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(request, timeout=25) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    res = _json(raw)
    if not res.get("success"):
        raise HttpError(f"换取会话失败：{res.get('message')}")
    session = ""
    for cookie in jar:
        if cookie.name == SESSION_KEY:
            session = cookie.value
    if not session or session == "deleteMe":
        raise HttpError("没拿到有效的 shiroJID 会话")
    return session, res.get("data") or {}


def _app_post(session: str, path: str, form: dict) -> dict:
    _, _, raw = _request(
        "POST", BASE_APP + path, form, {"Cookie": f"{SESSION_KEY}={session}"},
    )
    res = _json(raw)
    if not res.get("success"):
        status = res.get("statusCode")
        if status == 204:
            raise HttpError("会话已过期")
        raise HttpError(f"[{status}] {res.get('message')}")
    return res


def query_bind(session: str) -> list[dict]:
    """查绑定寝室；不同学校/厂商用不同 bindType（3=电量、1=电费、2=水费）。"""
    errors = []
    for bind_type in ("3", "1", "2"):
        try:
            res = _app_post(session, "/app/electric/queryBind", {"bindType": bind_type})
        except HttpError as exc:
            errors.append(str(exc))
            continue
        rows = res.get("rows") or []
        if rows:
            return rows
    if errors:
        raise HttpError("；".join(errors[:2]))
    return []


def query_surplus(session: str, room: dict) -> dict:
    form = {
        "areaId": room.get("areaId"),
        "buildingCode": room.get("buildingCode"),
        "floorCode": room.get("floorCode"),
        "roomCode": room.get("roomCode"),
    }
    last: Exception | None = None
    for path in ("/app/electric/queryISIMSRoomSurplus", "/app/electric/queryRoomSurplus"):
        try:
            res = _app_post(session, path, form)
            data = res.get("data") or {}
            if data:
                return data
        except HttpError as exc:
            last = exc
    if last:
        raise last
    raise HttpError("没有返回电量数据（学校可能没接电量模块）")


def surplus_view(data: dict, unit: str = "") -> tuple[str, float | None, str, str]:
    """统一两种返回结构，返回 (房间名, 数值, 单位, 展示文本)。"""
    room = data.get("displayRoomName") or ""
    parts: list[str] = []
    value: float | None = None
    if data.get("soc") is not None:
        value, unit = float(data["soc"]), "度"
        parts.append(f"剩余 {value} 度")
        if data.get("totalSocAmount") is not None:
            parts.append(f"约 ￥{data['totalSocAmount']}")
        detail = data.get("surplusList") or []
        if detail and detail[0].get("roomStatus"):
            parts.append(f"状态：{detail[0]['roomStatus']}")
    elif data.get("surplus") is not None:
        value = float(data["surplus"])
        if not unit:
            unit = "度" if data.get("isShowSurplus") == 1 and data.get("isShowMoney") != 1 else "元"
        parts.append(f"剩余 {value} {unit}")
        if data.get("remind"):
            parts.append(f"备注：{data['remind']}")
    return room, value, unit, "，".join(parts)


# ======================================================================
# 最小 WebSocket 客户端（RFC6455，仅客户端/文本帧）
# ======================================================================
class WSClosed(RuntimeError):
    pass


class IdleTimeout(RuntimeError):
    pass


class WebSocket:
    """够用就好的 WebSocket 客户端：文本帧、ping/pong、close。"""

    def __init__(self, url: str, timeout: float = 30.0, ua: str = "moviepilot-yxy/1.0"):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"不支持的 URL: {url}")
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.secure = parsed.scheme == "wss"
        self.timeout = timeout
        self.ua = ua
        self.sock = None
        self._buf = b""
        self._frag: list[bytes] = []

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {self.ua}\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSClosed("握手期间连接被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise WSClosed(f"握手失败：{status_line}")

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise WSClosed("未连接")
        length = len(payload)
        frame = bytes([0x80 | opcode])
        if length < 126:
            frame += bytes([0x80 | length])
        elif length < 65536:
            frame += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            frame += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(frame)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        self.sock = None

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSClosed("连接被关闭")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def has_pending(self, idle: float = 1.0) -> bool:
        """判断有没有数据可读；没有就等一小会儿（用于同线程里做定时任务）。"""
        if self._buf:
            return True
        try:
            if self.sock.pending():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            ready, _, _ = select.select([self.sock], [], [], idle)
            return bool(ready)
        except (OSError, ValueError):
            raise WSClosed("连接已关闭")

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WSClosed("服务端关闭连接")
            if opcode in (0x1, 0x2):
                self._frag = [payload]
                if fin:
                    return b"".join(self._frag).decode("utf-8", "replace")
                continue
            if opcode == 0x0:
                self._frag.append(payload)
                if fin:
                    data = b"".join(self._frag)
                    self._frag = []
                    return data.decode("utf-8", "replace")
                continue


# ======================================================================
# 机器人工作线程：长连接 + 定时查余额
# ======================================================================
class BotWorker(threading.Thread):
    """一个线程同时负责：收消息、回消息、心跳、定时查余额并告警。"""

    def __init__(self, plugin: "YiXiaoYuanElectricity"):
        super().__init__(name="YiXiaoYuanElectricityBot", daemon=True)
        self.plugin = plugin
        self._stop = threading.Event()
        self._seen: list[str] = []
        self.ws: WebSocket | None = None
        self.status = "未启动"
        self.last_error = ""
        self.last_alert_ts = 0.0
        self.last_check_ts = 0.0
        self.last_value: float | None = None
        self._next_alert_at = 0.0
        self._next_ping_at = 0.0
        self._last_recv = 0.0

    # --- 对外 -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def send_markdown(self, chatid: str, content: str, wait: bool = True) -> int | None:
        """主动推送（只支持 markdown + chatid）。"""
        if not self.ws or not self.plugin._bot_id:
            return None
        req = self._send_cmd(CMD_SEND, {
            "chatid": chatid, "msgtype": "markdown", "markdown": {"content": content},
        })
        if not wait:
            return 0
        ack = self._wait_ack(req, 6.0)
        return None if ack is None else int(ack.get("errcode", -1))

    # --- 连接 -------------------------------------------------------
    def run(self) -> None:
        backoff = 3
        while not self._stop.is_set():
            try:
                self._connect()
                backoff = 3
                self.status = "已连接"
                logger.info("[易校园电量] 机器人长连接已建立")
                self._loop()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.status = f"断开：{self.last_error}"
                logger.warning(f"[易校园电量] 机器人连接异常：{self.last_error}")
            finally:
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
            if self._stop.is_set():
                break
            self.status = f"{backoff} 秒后重连"
            self._stop.wait(backoff)
            backoff = min(60, backoff * 2)
        self.status = "已停止"

    def _connect(self) -> None:
        ws = WebSocket(WS_URL)
        ws.connect()
        self.ws = ws
        self._last_recv = time.time()
        self._send_cmd(CMD_SUBSCRIBE, {"bot_id": self.plugin._bot_id, "secret": self.plugin._bot_secret})
        self._next_ping_at = time.time() + 25
        self._next_alert_at = time.time() + 5

    def _send_cmd(self, cmd: str, body: dict | None = None) -> str:
        req_id = f"{cmd}_{int(time.time() * 1000)}_{random.randint(100, 999)}"
        payload: dict[str, Any] = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            payload["body"] = body
        if not self.ws:
            raise WSClosed("未连接")
        self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        return req_id

    def _wait_ack(self, req_id: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.ws:
                return None
            if not self.ws.has_pending(min(1.0, max(0.1, deadline - time.time()))):
                continue
            raw = self.ws.recv_text()
            self._last_recv = time.time()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if (msg.get("headers") or {}).get("req_id") == req_id and not msg.get("cmd"):
                return msg
            self._dispatch(raw)
        return None

    # --- 主循环 -----------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            if now >= self._next_ping_at:
                self._next_ping_at = now + 25
                self._send_cmd(CMD_PING)
            if now >= self._next_alert_at:
                self._next_alert_at = now + max(60, self.plugin._interval * 60)
                try:
                    self.check_balance()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[易校园电量] 定时查询失败：{exc}")
            if not self.ws:
                raise WSClosed("连接不存在")
            if not self.ws.has_pending(1.0):
                if time.time() - self._last_recv > 120:
                    raise WSClosed("超过 120 秒没有收到任何数据")
                continue
            raw = self.ws.recv_text()
            self._last_recv = time.time()
            self._dispatch(raw)

    def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        cmd = msg.get("cmd") or ""
        headers = msg.get("headers") or {}
        body = msg.get("body") or {}
        if not cmd:
            errcode = msg.get("errcode", 0)
            if str(headers.get("req_id", "")).startswith(CMD_SUBSCRIBE):
                if errcode == 0:
                    self.status = "已连接（订阅成功）"
                    logger.info("[易校园电量] 机器人订阅成功")
                else:
                    self.status = f"订阅失败 {errcode}: {msg.get('errmsg')}"
                    logger.error(f"[易校园电量] 机器人订阅失败 {errcode} {msg.get('errmsg')}")
                    self._stop.set()
            elif errcode:
                logger.warning(f"[易校园电量] 回包异常 {errcode}: {msg.get('errmsg')}")
            return
        if cmd == CMD_CALLBACK:
            self._on_message(headers.get("req_id", ""), body)
        elif cmd == CMD_EVENT:
            logger.debug(f"[易校园电量] 收到事件：{json.dumps(body, ensure_ascii=False)[:200]}")

    # --- 消息处理 ---------------------------------------------------
    def _on_message(self, req_id: str, body: dict) -> None:
        msgid = str(body.get("msgid") or "")
        if msgid:
            if msgid in self._seen:
                return
            self._seen = (self._seen + [msgid])[-200:]

        chattype = body.get("chattype") or "single"
        chatid = body.get("chatid") or ""
        userid = str((body.get("from") or {}).get("userid") or "")
        msgtype = body.get("msgtype") or ""
        content = ""
        for key in ("text", "markdown", "voice"):
            if msgtype == key:
                content = str((body.get(key) or {}).get("content") or "")

        if chatid and chatid != self.plugin._chatid:
            self.plugin._chatid = chatid
            self.plugin._persist()
            logger.info(f"[易校园电量] 记录群会话 chatid={chatid[-8:]}（{chattype}）")

        text = self._strip_mentions(content)
        logger.info(f"[易校园电量] 收到消息 user={userid or '-'} chat={chattype} 内容={text!r}")

        allowed = self.plugin._allowed_users()
        if allowed and userid not in allowed:
            self._reply(req_id, f"抱歉，{userid or '你'} 没有查询权限。\n"
                                f"管理员可在插件配置的「允许使用的人」里加上你的 userid。")
            return

        try:
            answer = self._dispatch_command(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[易校园电量] 处理消息失败：{exc}")
            answer = f"查询失败：{exc}"
        self._reply(req_id, answer)

    @staticmethod
    def _strip_mentions(text: str) -> str:
        cleaned = (text or "").replace("\u2005", " ").replace("\u00a0", " ")
        kept = [p for p in cleaned.split() if not p.startswith("@")]
        return " ".join(kept) if kept else cleaned.strip()

    def _dispatch_command(self, text: str) -> str:
        plugin = self.plugin
        t = (text or "").strip()
        if not t or t.lower() in ("帮助", "help", "?", "？", "菜单"):
            room = (plugin._room or {}).get("displayName") or "未绑定"
            return ("我是寝室电费查询助手，可以直接 @ 我：\n\n"
                    "· **查电量** / 余额 —— 查当前余额\n"
                    "· **阈值 30** —— 改提醒阈值\n"
                    "· **状态** —— 看当前配置\n"
                    "· **帮助** —— 这条消息\n\n"
                    f"当前寝室：{room}\n"
                    f"当前阈值：{plugin._threshold} {plugin._unit or ''}")
        if "阈值" in t:
            matched = re.search(r"(?:阈值|低于)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", t)
            if not matched:
                return f"用法：阈值 30\n当前阈值：{plugin._threshold} {plugin._unit or ''}"
            plugin._threshold = float(matched.group(1))
            plugin._persist()
            return f"已把提醒阈值改成 **{plugin._threshold}** {plugin._unit or ''}。"
        if "状态" in t:
            room = (plugin._room or {}).get("displayName") or "未绑定"
            return (f"寝室：{room}\n阈值：{plugin._threshold} {plugin._unit or ''}\n"
                    f"轮询：每 {plugin._interval} 分钟\n"
                    f"学校：{plugin._school_name}（{plugin._school_code}）")
        return self._describe_balance()

    def _describe_balance(self) -> str:
        plugin = self.plugin
        session = plugin._ensure_session()
        if not plugin._room:
            rows = query_bind(session)
            if not rows:
                return "这个账号没有绑定寝室电量，先在易校园 App 里绑定「我的宿舍」。"
            plugin._set_room(rows[0])
        data = query_surplus(session, plugin._room)
        room_name, value, unit, text = surplus_view(data, plugin._unit)
        if value is None:
            return f"{room_name} 没有返回数值：{json.dumps(data, ensure_ascii=False)[:200]}"
        icon = "🔴" if value < plugin._threshold else "🟢"
        lines = [f"{icon} **{room_name or plugin._room.get('displayName')}**", text]
        left = value - plugin._threshold
        lines.append(f"已低于阈值 **{plugin._threshold}** {unit}，建议尽快充值" if left < 0
                     else f"距阈值 {plugin._threshold} {unit} 还有 {round(left, 2)}")
        lines.append(f"<font color=\"comment\">{time.strftime('%Y-%m-%d %H:%M:%S')}</font>")
        return "\n".join(lines)

    def _reply(self, req_id: str, text: str) -> None:
        """stream 回复：先发占位帧，等到 ack 再发正文（连发会被判版本冲突）。"""
        stream_id = f"stream_{os.urandom(4).hex()}"
        try:
            self._send_cmd(CMD_RESPOND, {"msgtype": "stream", "stream": {
                "id": stream_id, "content": "<think></think>", "finish": False}})
            self._wait_ack(req_id, 3.0)
            self._send_cmd(CMD_RESPOND, {"msgtype": "stream", "stream": {
                "id": stream_id, "content": text, "finish": True}})
            self._wait_ack(req_id, 3.0)
            logger.info(f"[易校园电量] 已回复 {len(text)} 字")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[易校园电量] 回复失败：{exc}")

    # --- 定时告警 ---------------------------------------------------
    def check_balance(self) -> None:
        plugin = self.plugin
        self.last_check_ts = time.time()
        session = plugin._ensure_session()
        if not plugin._room:
            rows = query_bind(session)
            if not rows:
                logger.warning("[易校园电量] 没有绑定寝室，跳过")
                return
            plugin._set_room(rows[0])
        data = query_surplus(session, plugin._room)
        room_name, value, unit, text = surplus_view(data, plugin._unit)
        self.last_value = value
        if value is None:
            logger.warning(f"[易校园电量] 响应里没有数值：{json.dumps(data, ensure_ascii=False)[:200]}")
            return
        room_name = room_name or plugin._room.get("displayName") or "寝室"
        logger.info(f"[易校园电量] 当前 {room_name} {text}（阈值 {plugin._threshold} {unit}）")

        low = value < plugin._threshold
        now = time.time()
        re_alert = max(1.0, float(plugin._re_alert_hours)) * 3600
        if low and (not plugin._low_state or now - self.last_alert_ts > re_alert):
            if not plugin._chatid:
                logger.warning("[易校园电量] 余额已低于阈值，但还没有群会话 ID，先在群里 @ 一次机器人")
                return
            markdown = (
                f"⚠️ **寝室余额不足提醒**\n\n"
                f"**{room_name}**\n\n"
                f"- 当前：**{value} {unit}**\n"
                f"- 阈值：{plugin._threshold} {unit}\n\n"
                f"建议尽快充值，免得突然断电。\n\n"
                f"<font color=\"comment\">{time.strftime('%Y-%m-%d %H:%M:%S')}</font>"
            )
            code = self.send_markdown(plugin._chatid, markdown)
            logger.info(f"[易校园电量] 已推送低余额告警（errcode={code}）")
            if code == 0:
                self.last_alert_ts = now
        elif not low and plugin._low_state:
            logger.info(f"[易校园电量] 余额已恢复：{value} {unit}")
        plugin._low_state = low
        plugin._persist()


# ======================================================================
# 插件主体
# ======================================================================
class YiXiaoYuanElectricity(_PluginBase):
    """易校园寝室电费监控：@ 机器人查询 + 低余额自动提醒。"""

    plugin_name = "易校园寝室电量"
    plugin_desc = "查询易校园绑定的寝室电费/电量，@企业微信机器人即可查询，低于阈值自动提醒。"
    plugin_icon = "YiXiaoYuanElectricity.png"
    plugin_version = "1.0.5"
    plugin_author = "hahmyy"
    author_url = "https://github.com/hahmyy"
    plugin_config_prefix = "yixiaoyuanelectricity_"
    plugin_order = 40
    auth_level = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 配置
        self._enabled = False
        self._phone = ""
        self._code = ""
        self._captcha = ""
        self._uid_input = ""
        self._bot_id = ""
        self._bot_secret = ""
        self._threshold = 20.0
        self._interval = 30
        self._allowed = ""
        self._unit = ""
        self._re_alert_hours = 6
        # 运行态
        self._uid = ""
        self._session = ""
        self._device_id = ""
        self._sec_token = ""
        self._need_captcha = False
        self._sec_token_ts = 0.0
        self._captcha_png_b64 = ""
        self._chatid = ""
        self._room: dict | None = None
        self._school_name = ""
        self._school_code = ""
        self._low_state = False
        self._last_alert_ts = 0.0
        self._worker: BotWorker | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict | None = None) -> None:
        self._stop_worker()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._phone = str(config.get("phone") or "").strip()
        self._code = str(config.get("code") or "").strip()
        self._captcha = str(config.get("captcha") or "").strip()
        self._uid_input = str(config.get("uid") or "").strip()
        self._bot_id = str(config.get("bot_id") or "").strip()
        self._bot_secret = str(config.get("bot_secret") or "").strip()
        try:
            self._threshold = float(config.get("threshold") or 20)
        except (TypeError, ValueError):
            self._threshold = 20.0
        try:
            self._interval = max(5, int(config.get("interval") or 30))
        except (TypeError, ValueError):
            self._interval = 30
        self._allowed = str(config.get("allowed_users") or "").strip()
        self._unit = str(config.get("unit") or "").strip()
        try:
            self._re_alert_hours = float(config.get("re_alert_hours") or 6)
        except (TypeError, ValueError):
            self._re_alert_hours = 6.0

        state = self._load_state()
        self._uid = state.get("uid", "")
        self._session = state.get("session", "")
        self._device_id = state.get("device_id", "")
        self._chatid = state.get("chatid", "")
        self._room = state.get("room")
        self._school_name = state.get("school_name", "")
        self._school_code = state.get("school_code", "")
        self._low_state = bool(state.get("low", False))
        self._last_alert_ts = float(state.get("last_alert_ts", 0) or 0)
        self._captcha_png_b64 = state.get("captcha_png") or ""
        self._sec_token = state.get("sec_token") or self._sec_token
        self._sec_token_ts = float(state.get("sec_token_ts") or 0)

        # 配置页里手填的 UID 优先（跳过短信登录的兜底路径）
        if self._uid_input and self._uid_input != self._uid:
            self._uid = self._uid_input
            self._session = ""
            self._room = None
            self._persist()

        if self._enabled and self._bot_id and self._bot_secret:
            self._start_worker()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        self._stop_worker()

    def _start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = BotWorker(self)
        self._worker.last_alert_ts = self._last_alert_ts
        self._worker.start()
        logger.info("[易校园电量] 机器人后台线程已启动")

    def _stop_worker(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None
            logger.info("[易校园电量] 机器人后台线程已停止")

    # ------------------------------------------------------------------
    # 配置 / 状态持久化
    # ------------------------------------------------------------------
    def _current_config(self) -> dict:
        return {
            "enabled": self._enabled,
            "phone": self._phone,
            # 验证码/图形验证码也存进配置：表单模型里始终有这些 key，
            # 页面刷新或保存后输入框不会丢值，按钮里 model.code 一定有定义
            "code": self._code,
            "captcha": self._captcha,
            "uid": self._uid,
            "bot_id": self._bot_id,
            "bot_secret": self._bot_secret,
            "threshold": self._threshold,
            "interval": self._interval,
            "allowed_users": self._allowed,
            "unit": self._unit,
            "re_alert_hours": self._re_alert_hours,
            "onlyonce": False,
        }

    def _load_state(self) -> dict:
        try:
            data = self.get_data("state")
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
        try:
            path = self.get_data_path() / "state.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _persist(self) -> None:
        state = {
            "uid": self._uid,
            "session": self._session,
            "device_id": self._device_id,
            "chatid": self._chatid,
            "room": self._room,
            "school_name": self._school_name,
            "school_code": self._school_code,
            "low": self._low_state,
            "last_alert_ts": self._last_alert_ts,
            "captcha_png": self._captcha_png_b64,
            "sec_token": self._sec_token,
            "sec_token_ts": self._sec_token_ts,
        }
        try:
            self.save_data("state", state)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            path = self.get_data_path() / "state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[易校园电量] 保存状态失败：{exc}")

    def _set_room(self, row: dict) -> None:
        self._room = {
            "areaId": row.get("areaId"),
            "buildingCode": row.get("buildingCode"),
            "floorCode": row.get("floorCode"),
            "roomCode": row.get("roomCode"),
            "displayName": row.get("displayName") or " ".join(filter(None, [
                row.get("areaName"), row.get("buildingName"),
                row.get("floorName"), row.get("roomName"),
            ])),
        }
        self._persist()

    def _allowed_users(self) -> list[str]:
        return [x.strip() for x in re.split(r"[,，\s]+", self._allowed) if x.strip()]

    def _ensure_session(self) -> str:
        """拿 shiroJID：有缓存先用，过期再换；换失败抛错。"""
        if not self._uid:
            raise HttpError("还没有登录：先在配置页用手机号 + 验证码登录")
        if self._session:
            try:
                query_bind(self._session)
                return self._session
            except HttpError as exc:
                if "过期" not in str(exc):
                    raise
        session, info = oauth_authorize(self._uid)
        self._session = session
        if info:
            self._school_name = info.get("schoolName") or self._school_name
            self._school_code = info.get("schoolCode") or self._school_code
        self._persist()
        return session

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------
    @staticmethod
    def _btn(label: str, path: str, payload: str, color: str = "primary",
             icon: str = "mdi-play", block: bool = False, confirm: str = "") -> dict:
        """生成一个调用插件接口的按钮（前端 JS 里 model 是当前表单值）。"""
        body = payload
        pre = f"if (!confirm('{confirm}')) return; " if confirm else ""
        js = (
            "function(e) { " + pre +
            f"window.MoviePilotAPI.post('plugin/YiXiaoYuanElectricity/{path}', {body})"
            ".then(function(r) { alert((r && (r.msg || r.data)) || '完成') })"
            ".catch(function(err) { console.error(err); alert('请求失败，请查看日志') }) }"
        )
        return {
            "component": "VBtn",
            "props": {
                "color": color,
                "variant": "flat",
                "block": block,
                "prepend-icon": icon,
                "onclick": js,
            },
            "text": label,
        }

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "class": "mb-2",
                                    "text": "第一步用手机号 + 验证码登录易校园（只需一次）；"
                                            "第二步填企业微信智能机器人的 BotID / Secret；"
                                            "第三步测试通过后启用插件。",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6},
                            "content": [
                                {"component": "VSwitch",
                                 "props": {"model": "enabled", "label": "启用插件"}},
                            ],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VDivider",
                                "props": {"class": "my-2"},
                            }, {
                                "component": "VSubheader",
                                "props": {"class": "text-subtitle-2"},
                                "text": "1. 易校园登录",
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 8},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "phone",
                                                       "label": "手机号（易校园登录用）",
                                                       "placeholder": "13800000000",
                                                       "persistent-hint": True,
                                                       "hint": "填完先点右边的「发送验证码」"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [self._btn("发送验证码", "send_code",
                                                      "{phone: model.phone, captcha: String(model.captcha || '')}",
                                                      icon="mdi-message-text")],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": self._captcha_image_component(),
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 8},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "captcha",
                                                       "label": "图形验证码（易校园要求时必填）",
                                                       "placeholder": "看上方图片，把字符填这里",
                                                       "persistent-hint": True,
                                                       "hint": "短信发太频繁会被风控：提示「需要图形验证码」时，"
                                                               "先点右边按钮拉图，关掉配置窗口再打开就能看到图片，"
                                                               "填好字符再点发送验证码"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [self._btn("获取图形验证码", "captcha", "{}",
                                                      icon="mdi-image-search")],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 8},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "code",
                                                       "label": "短信验证码（收到后填这里）",
                                                       "placeholder": "6 位数字",
                                                       "maxlength": 6,
                                                       "persistent-hint": True,
                                                       "hint": "验证码 5 分钟内有效；填完点右边的「登录并绑定寝室」"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [self._btn("登录并绑定寝室", "verify",
                                                      "{phone: model.phone, code: String(model.code || '')}",
                                                      icon="mdi-login")],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 8},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "uid",
                                                       "label": "已经有 UID 的话可以直接填（跳过短信）",
                                                       "placeholder": "例如 2609237995132109031",
                                                       "persistent-hint": True,
                                                       "hint": "UID 就是易校园抓包里的 ymId；填了它就不需要验证码"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [self._btn("用 UID 登录", "set_uid",
                                                      "{uid: String(model.uid || '')}",
                                                      color="secondary", icon="mdi-key-variant")],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VDivider",
                                "props": {"class": "my-2"},
                            }, {
                                "component": "VSubheader",
                                "props": {"class": "text-subtitle-2"},
                                "text": "2. 企业微信智能机器人（API 模式 + 长连接，不需要公网）",
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 6},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "bot_id", "label": "BotID"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 6},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "bot_secret",
                                                       "label": "Secret",
                                                       "type": "password"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 6},
                                "content": [self._btn("测试机器人连接", "test_bot", "{}",
                                                      icon="mdi-lan-connect")],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 6},
                                "content": [self._btn("测试获取电量", "test_query", "{}",
                                                      icon="mdi-flash")],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VDivider",
                                "props": {"class": "my-2"},
                            }, {
                                "component": "VSubheader",
                                "props": {"class": "text-subtitle-2"},
                                "text": "3. 提醒设置",
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "threshold",
                                                       "label": "低于多少提醒",
                                                       "type": "number",
                                                       "suffix": "元/度"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "interval",
                                                       "label": "查询间隔（分钟）",
                                                       "type": "number"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [{"component": "VTextField",
                                             "props": {"model": "unit",
                                                       "label": "单位（留空自动判断）",
                                                       "placeholder": "元 / 度"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {"model": "allowed_users",
                                          "label": "允许使用的人（企业微信 userid，逗号分隔；留空=所有人）",
                                          "placeholder": "ZhangSan,LiSi"},
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [self._btn("发一条测试提醒到群", "test_push", "{}",
                                                  color="warning", icon="mdi-bell-ring-outline",
                                                  block=True)],
                        }],
                    },
                ],
            }
        ]
        default = self._current_config()
        default["code"] = ""
        return form, default

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------
    def get_page(self) -> list[dict]:
        state = self._load_state()
        room = (state.get("room") or {}).get("displayName") or "未绑定"
        worker = self._worker
        running = bool(worker and worker.is_alive())
        status = worker.status if worker else "未运行"
        last_value = worker.last_value if worker else None
        last_check = worker.last_check_ts if worker else 0
        items = [
            ("机器人状态", status, "mdi-robot" if running else "mdi-robot-off",
             "success" if running else "warning"),
            ("账号", f"{state.get('school_name') or '—'}"
                     f"{'（已登录）' if state.get('uid') else '（未登录）'}", "mdi-account", "info"),
            ("寝室", room, "mdi-home", "info"),
            ("最近读数", f"{last_value} {self._unit or ''}" if last_value is not None else "—",
             "mdi-flash", "success" if (last_value or 0) >= self._threshold else "error"),
            ("最后查询", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_check))
             if last_check else "—", "mdi-clock-outline", "info"),
            ("提醒阈值", f"{self._threshold} {self._unit or ''}", "mdi-bell-alert", "info"),
        ]
        cards = []
        for label, value, icon, color in items:
            cards.append({
                "component": "VCol",
                "props": {"cols": 12, "sm": 6, "md": 4},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color, "class": "pa-3 h-100"},
                    "content": [{
                        "component": "VCardText",
                        "content": [
                            {"component": "VIcon", "props": {"icon": icon, "size": "small"}},
                            {"component": "div", "props": {"class": "text-caption mt-1"}, "text": label},
                            {"component": "div", "props": {"class": "text-body-1 font-weight-medium"},
                             "text": str(value)},
                        ],
                    }],
                }],
            })
        page = [{"component": "VRow", "content": cards}]
        if not self._bot_id or not self._bot_secret:
            page.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mt-3",
                "text": "还没填 BotID / Secret，机器人不会启动。"}})
        if not state.get("uid"):
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-3",
                "text": "还没登录易校园：在配置页填手机号，点「发送验证码」，再点「登录并绑定寝室」。"}})
        if state.get("uid") and not state.get("chatid"):
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mt-3",
                "text": "还没记录群会话：把机器人拉进群，@ 它发一句「查电量」，之后才能主动推送低余额提醒。"}})
        return page

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> list[dict[str, Any]]:
        return [
            {"path": "/send_code", "endpoint": self.api_send_code, "methods": ["POST"],
             "auth": "bear", "summary": "发送易校园登录验证码"},
            {"path": "/captcha", "endpoint": self.api_captcha, "methods": ["POST"],
             "auth": "bear", "summary": "获取易校园图形验证码"},
            {"path": "/verify", "endpoint": self.api_verify, "methods": ["POST"],
             "auth": "bear", "summary": "验证码登录并绑定寝室"},
            {"path": "/set_uid", "endpoint": self.api_set_uid, "methods": ["POST"],
             "auth": "bear", "summary": "用 UID 直接登录（跳过短信）"},
            {"path": "/test_bot", "endpoint": self.api_test_bot, "methods": ["POST"],
             "auth": "bear", "summary": "测试企业微信机器人长连接"},
            {"path": "/test_query", "endpoint": self.api_test_query, "methods": ["POST"],
             "auth": "bear", "summary": "测试查询寝室余额"},
            {"path": "/test_push", "endpoint": self.api_test_push, "methods": ["POST"],
             "auth": "bear", "summary": "发送一条测试提醒"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询插件当前状态"},
        ]

    @staticmethod
    async def _read_payload(request: Request | None = None) -> dict:
        """从请求里取参数：JSON body / 表单 body / query 都兼容。

        之前用 `payload: dict = None` 由 FastAPI 做 body 推断，在部分宿主版本上会
        变成 422（前端显示「请求参数不正确」），所以这里自己读 body，不依赖签名推断。
        """
        data: dict[str, Any] = {}
        if request is None:
            return data
        try:
            for key, value in request.query_params.items():
                data[key] = value
        except Exception:  # noqa: BLE001
            pass
        try:
            body = await request.body()
        except Exception:  # noqa: BLE001
            body = b""
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    data.update(parsed)
            except Exception:  # noqa: BLE001
                try:
                    data.update(dict(urllib.parse.parse_qsl(body.decode("utf-8", "ignore"))))
                except Exception:  # noqa: BLE001
                    pass
        nested = data.get("payload")
        if isinstance(nested, dict):
            data.update(nested)
            data.pop("payload", None)
        return data

    async def api_send_code(self, request: Request = None) -> dict:
        """发短信验证码；易校园风控要求时带上图形验证码。

        关键点：图形验证码是绑在**那一个** securityToken 上的。取图时用的是 token A，
        发短信时如果又去换了一个 token B，服务端就会判定「验证码错误」（bizCode 10040）。
        所以这里必须复用取图时的 token，只有过期或没有时才重新取。
        """
        data = await self._read_payload(request)
        phone = str(data.get("phone") or self._phone).strip()
        captcha = str(data.get("captcha") or self._captcha or "").strip()
        if not phone:
            return {"code": 1, "msg": "请先填手机号"}
        try:
            if not self._device_id:
                self._device_id = gen_device_id()
                self._persist()

            fresh = (not self._sec_token) or (time.time() - self._sec_token_ts > 300)
            if fresh:
                info = get_security_info(self._device_id)
                self._sec_token = info["securityToken"]
                self._sec_token_ts = time.time()
                self._need_captcha = info["level"] != 0
                logger.info(f"[易校园电量] 换了新的 securityToken，level={info['level']}")
            if self._need_captcha and not captcha:
                return {
                    "code": 2,
                    "msg": "易校园要求图形验证码（短信发太频繁触发的风控）。\n"
                           "先点「获取图形验证码」，关掉配置窗口再打开就能看到图，"
                           "把字符填到「图形验证码」里，再点发送验证码。",
                }
            result = send_login_code(phone, self._device_id, self._sec_token, captcha)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            logger.warning(f"[易校园电量] 发送验证码失败：{msg}")
            if "10040" in msg or "验证码错误" in msg:
                self._reset_captcha()
                return {
                    "code": 1,
                    "msg": "图形验证码不对或已过期（bizCode 10040）。\n"
                           "再点一次「获取图形验证码」，关掉配置窗口重新打开看图，"
                           "填新的字符后马上点发送验证码。",
                }
            return {"code": 1, "msg": f"发送失败：{msg}"}
        # 成功后这张图就用掉了，清掉避免下次误用
        self._reset_captcha()
        hint = "" if result.get("userExists") is not False else "（该号码可能还没注册易校园）"
        return {"code": 0, "msg": f"验证码已发送到 {phone}{hint}，收到后填到「短信验证码」里再点登录"}

    def _reset_captcha(self) -> None:
        """作废当前图形验证码与它绑定的 securityToken。"""
        self._sec_token = ""
        self._sec_token_ts = 0.0
        self._captcha_png_b64 = ""
        self._persist()

    async def api_captcha(self, request: Request = None) -> dict:
        """拉一张图形验证码存起来，并返回 base64（供配置页直接内嵌显示）。

        不走自定义 GET 图片路由：插件 API 里返回原生 Response 在部分宿主版本上
        注册不成功（表现为 404），所以改成「拉图 -> 存状态 -> 配置页重新打开时内嵌」。
        """
        try:
            if not self._device_id:
                self._device_id = gen_device_id()
            info = get_security_info(self._device_id)
            self._sec_token = info["securityToken"]
            self._sec_token_ts = time.time()
            self._need_captcha = info["level"] != 0
            png = get_captcha_png(self._device_id, self._sec_token)
            self._captcha_png_b64 = base64.b64encode(png).decode("ascii")
            self._persist()
            logger.info(f"[易校园电量] 已拉取图形验证码（level={info['level']}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[易校园电量] 取图形验证码失败：{exc}")
            return {"code": 1, "msg": f"获取图形验证码失败：{exc}"}
        return {
            "code": 0,
            "msg": "图形验证码已获取。\n配置窗口关掉再重新打开一次，图片会直接显示在「图形验证码」上方；"
                   "看到图后把字符填进「图形验证码」，再点发送验证码。",
        }

    def _captcha_image_component(self) -> list[dict]:
        """把已保存的图形验证码渲染成配置页里的图片（没有就提示先获取）。"""
        if self._captcha_png_b64:
            return [{
                "component": "VImg",
                "props": {
                    "src": "data:image/png;base64," + self._captcha_png_b64,
                    "max-width": 160,
                    "class": "mb-2 border",
                    "alt": "易校园图形验证码",
                },
            }]
        return [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "density": "compact",
                "class": "mb-2",
                "text": "需要图形验证码时，先点右边的「获取图形验证码」，"
                        "然后关掉配置窗口再重新打开，图片会显示在这里。",
            },
        }]

    def _complete_login(self, user: dict) -> None:
        """登录成功后统一收尾：换会话、查寝室、落盘。"""
        self._uid = str(user.get("id") or "")
        self._school_name = user.get("schoolName") or ""
        self._school_code = user.get("schoolCode") or ""
        self._session, _ = oauth_authorize(self._uid)
        rows = query_bind(self._session)
        if rows:
            self._set_room(rows[0])
        else:
            self._room = None
        self._persist()
        self.update_config(self._current_config())

    async def api_verify(self, request: Request = None) -> dict:
        data = await self._read_payload(request)
        phone = str(data.get("phone") or self._phone).strip()
        # 验证码优先用本次请求带的，其次用配置里已保存的（页面刷新后还在）
        code = str(data.get("code") or self._code or "").strip()
        if not phone:
            return {"code": 1, "msg": "请先填手机号并点「发送验证码」"}
        if not code:
            return {"code": 1, "msg": "请把收到的短信验证码填到「短信验证码」输入框里"}
        try:
            if not self._device_id:
                self._device_id = gen_device_id()
            user = verify_login(phone, self._device_id, code)
            self._phone = phone
            self._complete_login(user)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[易校园电量] 登录失败：{exc}")
            return {"code": 1, "msg": f"登录失败：{exc}"}
        room = (self._room or {}).get("displayName") or "未绑定寝室"
        return {"code": 0, "msg": f"登录成功：{self._school_name} · {room}\nUID {self._uid}"}

    async def api_set_uid(self, request: Request = None) -> dict:
        data = await self._read_payload(request)
        uid = str(data.get("uid") or self._uid_input or "").strip()
        if not uid:
            return {"code": 1, "msg": "请先填 UID"}
        try:
            session, info = oauth_authorize(uid)
            self._uid = uid
            self._uid_input = uid
            self._session = session
            self._school_name = info.get("schoolName") or ""
            self._school_code = info.get("schoolCode") or ""
            rows = query_bind(session)
            if rows:
                self._set_room(rows[0])
            else:
                self._room = None
            self._persist()
            self.update_config(self._current_config())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[易校园电量] UID 登录失败：{exc}")
            return {"code": 1, "msg": f"UID 登录失败：{exc}"}
        room = (self._room or {}).get("displayName") or "未绑定寝室"
        return {"code": 0, "msg": f"UID 登录成功：{self._school_name} · {room}"}

    async def api_test_bot(self, request: Request = None) -> dict:
        data = await self._read_payload(request)
        bot_id = str(data.get("bot_id") or self._bot_id).strip()
        secret = str(data.get("bot_secret") or self._bot_secret).strip()
        if not bot_id or not secret:
            return {"code": 1, "msg": "请先填 BotID 和 Secret"}
        try:
            ws = WebSocket(WS_URL)
            ws.connect()
            ws.send_text(json.dumps({
                "cmd": CMD_SUBSCRIBE,
                "headers": {"req_id": "aibot_subscribe_test"},
                "body": {"bot_id": bot_id, "secret": secret},
            }, ensure_ascii=False))
            deadline = time.time() + 15
            ack = None
            while time.time() < deadline:
                if not ws.has_pending(1.0):
                    continue
                msg = json.loads(ws.recv_text())
                if not msg.get("cmd"):
                    ack = msg
                    break
            ws.close()
        except Exception as exc:  # noqa: BLE001
            return {"code": 1, "msg": f"连接失败：{exc}"}
        if not ack:
            return {"code": 1, "msg": "连接成功但没收到订阅回执"}
        errcode = ack.get("errcode", -1)
        if errcode == 0:
            return {"code": 0, "msg": "机器人连接正常（订阅成功）"}
        return {"code": 1, "msg": f"订阅失败 {errcode}：{ack.get('errmsg')}"}

    async def api_test_query(self, request: Request = None) -> dict:
        try:
            session = self._ensure_session()
            if not self._room:
                rows = query_bind(session)
                if not rows:
                    return {"code": 1, "msg": "这个账号没有绑定寝室电量（先在易校园 App 绑定「我的宿舍」）"}
                self._set_room(rows[0])
            data = query_surplus(session, self._room)
            room_name, value, unit, text = surplus_view(data, self._unit)
            if not self._unit and unit:
                self._unit = unit
            return {"code": 0, "msg": f"{room_name or self._room.get('displayName')}\n{text}"}
        except Exception as exc:  # noqa: BLE001
            return {"code": 1, "msg": f"查询失败：{exc}"}

    async def api_test_push(self, request: Request = None) -> dict:
        if not self._chatid:
            return {"code": 1, "msg": "还没有群会话 ID：先把机器人拉进群，@ 它发一句「查电量」"}
        if not (self._worker and self._worker.ws):
            return {"code": 1, "msg": "机器人还没连上，先启用插件或点「测试机器人连接」"}
        try:
            code = self._worker.send_markdown(
                self._chatid,
                "⚠️ **测试提醒**\n\n如果你看到这条，说明低余额提醒能正常推送。\n\n"
                f"<font color=\"comment\">{time.strftime('%Y-%m-%d %H:%M:%S')}</font>",
            )
        except Exception as exc:  # noqa: BLE001
            return {"code": 1, "msg": f"发送失败：{exc}"}
        if code == 0:
            return {"code": 0, "msg": "测试提醒已发送，去群里看看"}
        return {"code": 1, "msg": f"发送失败，errcode={code}"}

    async def api_status(self) -> dict:
        state = self._load_state()
        worker = self._worker
        return {
            "code": 0,
            "msg": "ok",
            "data": {
                "enabled": self._enabled,
                "logged_in": bool(state.get("uid")),
                "school": state.get("school_name"),
                "room": (state.get("room") or {}).get("displayName"),
                "chatid": bool(state.get("chatid")),
                "bot_running": bool(worker and worker.is_alive()),
                "bot_status": worker.status if worker else "未运行",
                "last_value": worker.last_value if worker else None,
                "last_check": worker.last_check_ts if worker else 0,
                "threshold": self._threshold,
            },
        }
