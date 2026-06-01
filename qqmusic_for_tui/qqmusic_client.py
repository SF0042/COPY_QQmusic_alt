#!/usr/bin/env python3
"""Small Music client for Linux.

This client uses metadata and playback-link APIs. It does not try to bypass
paid, region-locked, DRM-protected, or account-only content.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import hashlib
import json
import os
import random
import re
import shutil
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "https://api.ygking.top"
DEFAULT_TIMEOUT = 15
DEFAULT_AUTH_FILE = Path(__file__).with_name(".qqmusic_auth.json")
DEFAULT_NETEASE_AUTH_FILE = Path(__file__).with_name(".netease_auth.json")
DEFAULT_SETTINGS_FILE = Path(__file__).with_name(".qqmusic_settings.json")
PLAYLIST_INITIAL_PAGE_SIZE = 50
PLAYLIST_BACKGROUND_PAGE_SIZE = 500
QQ_QR_APPID = "716027609"
QQ_QR_THIRD_APPID = "100497308"
WX_QR_APPID = "wx48db31d50e334801"
WX_QR_REDIRECT_URI = "https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https%3A%2F%2Fy.qq.com%2F"
QQ_QR_REDIRECT_URI = "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https%3A%2F%2Fy.qq.com%2F"
NETEASE_BASE_URL = "https://music.163.com"
NETEASE_WEAPI_NONCE = "0CoJUm6Qyw8W8jud"
NETEASE_WEAPI_PUBKEY = "010001"
NETEASE_WEAPI_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee2559325"
    "75cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3"
    "ece0462db0a22b8e7"
)
NETEASE_WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)
NETEASE_DEVICE_XOR_KEY = "3go8&$8*3*3h0k(2)2"
PLATFORMS = {
    "qqmusic": "QQ 音乐",
    "netease": "网易云音乐",
}
PLAY_MODES = ("顺序播放", "随机播放", "单曲循环")
PLAY_MODE_ALIASES = {
    "order": "顺序播放",
    "sequence": "顺序播放",
    "sequential": "顺序播放",
    "normal": "顺序播放",
    "顺序": "顺序播放",
    "顺序播放": "顺序播放",
    "shuffle": "随机播放",
    "random": "随机播放",
    "rand": "随机播放",
    "随机": "随机播放",
    "随机播放": "随机播放",
    "repeat": "单曲循环",
    "repeat-one": "单曲循环",
    "single": "单曲循环",
    "one": "单曲循环",
    "loop": "单曲循环",
    "单曲": "单曲循环",
    "单曲循环": "单曲循环",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "platform": "qqmusic",
    "queue_show_singers": True,
    "queue_show_album": True,
    "queue_show_duration": True,
    "queue_show_mid": False,
    "queue_font_size": 11,
    "quality": "320",
    "play_mode": "顺序播放",
    "auto_sync_playlists": True,
    "download_dir": str(Path.home() / "音乐" / "Music"),
}


class QQMusicError(RuntimeError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass
class Song:
    title: str
    mid: str
    song_id: str = ""
    song_type: int = 0
    singers: str = ""
    album: str = ""
    duration: int | None = None
    media_mid: str = ""
    local_path: str = ""
    raw: dict[str, Any] | None = None

    @property
    def display(self) -> str:
        left = self.title
        if self.singers:
            left = f"{left} - {self.singers}"
        if self.album:
            left = f"{left}  [{self.album}]"
        if self.duration:
            left = f"{left}  {format_seconds(self.duration)}"
        return left


@dataclass
class Playlist:
    id: str
    name: str
    dirid: str = ""
    song_count: int | None = None
    cover: str = ""
    raw: dict[str, Any] | None = None

    @property
    def display(self) -> str:
        if self.song_count is None:
            return self.name
        return f"{self.name} ({self.song_count})"


@dataclass
class QRLoginSession:
    qrsig: str
    ptqrtoken: int
    image: bytes


@dataclass
class WXLoginSession:
    uuid: str
    image: bytes
    referer: str
    last_code: str = ""


@dataclass
class QRLoginResult:
    qq_number: str
    cookie: str
    nickname: str = ""


@dataclass
class LyricLine:
    time_ms: int
    text: str
    start_index: str = ""
    end_index: str = ""


def format_seconds(value: int) -> str:
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"


def format_milliseconds(value: int | float | None) -> str:
    if value is None:
        return "0:00"
    return format_seconds(max(0, int(value) // 1000))


def strip_lrc_timestamps(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        while line.startswith("[") and "]" in line:
            line = line[line.index("]") + 1 :]
        if line.strip():
            cleaned.append(line)
    return "\n".join(cleaned)


def parse_lrc_lines(text: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    timestamp_pattern = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for raw_line in text.splitlines():
        matches = list(timestamp_pattern.finditer(raw_line))
        if not matches:
            continue
        lyric = timestamp_pattern.sub("", raw_line).strip()
        if not lyric:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            milliseconds = int(fraction.ljust(3, "0")[:3])
            entries.append(((minutes * 60 + seconds) * 1000 + milliseconds, lyric))
    return sorted(entries, key=lambda item: item[0])


def decode_possible_base64(text: str) -> str:
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return text


def parse_json_like_response(body: bytes) -> dict[str, Any]:
    texts = [
        body.decode("utf-8", "replace").strip(),
        body.decode("gb18030", "replace").strip(),
    ]
    for text in texts:
        candidates = [text]
        match = re.search(r"\((\{.*\})\)\s*;?$", text, flags=re.S)
        if match:
            candidates.append(match.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    preview = texts[-1][:200] if texts else ""
    raise QQMusicError(f"接口返回的不是 JSON: {preview}")


def cookie_pairs(header_values: list[str]) -> list[str]:
    pairs: list[str] = []
    for header in header_values:
        pair = header.split(";", 1)[0].strip()
        if "=" in pair and not pair.endswith("="):
            pairs.append(pair)
    return pairs


def merge_cookie_pairs(existing: list[str], new_pairs: list[str]) -> list[str]:
    merged: dict[str, str] = {}
    for pair in [*existing, *new_pairs]:
        key, _, value = pair.partition("=")
        if key and value:
            merged[key] = value
    return [f"{key}={value}" for key, value in merged.items()]


def cookie_value(cookie: str, key: str) -> str:
    match = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)", cookie)
    return match.group(1) if match else ""


def hash33(value: str) -> int:
    result = 0
    for char in value:
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def get_gtk(p_skey: str) -> int:
    result = 5381
    for char in p_skey:
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def gtk_from_cookie(cookie: str) -> int:
    p_skey = cookie_value(cookie, "p_skey") or cookie_value(cookie, "skey")
    return get_gtk(p_skey) if p_skey else 5381


def musicu_login_fields(cookie: str) -> dict[str, str]:
    tme_login_type = cookie_value(cookie, "tmeLoginType")
    if tme_login_type:
        return {"tmeAppID": "qqmusic", "tmeLoginType": tme_login_type}
    if cookie_value(cookie, "login_type") == "2" or cookie_value(cookie, "wxuin"):
        return {"tmeAppID": "qqmusic", "tmeLoginType": "1"}
    return {}


def platform_display_name(platform: str) -> str:
    return PLATFORMS.get(platform, PLATFORMS["qqmusic"])


def normalize_platform(value: str | None) -> str:
    return value if value in PLATFORMS else "qqmusic"


def normalize_play_mode(value: str | None) -> str:
    if not value:
        return PLAY_MODES[0]
    return PLAY_MODE_ALIASES.get(value.strip().lower(), value if value in PLAY_MODES else PLAY_MODES[0])


def netease_weapi_encrypt(payload: dict[str, Any]) -> dict[str, str]:
    def aes_encrypt(text: str, key: str) -> str:
        pad_size = 16 - len(text.encode("utf-8")) % 16
        padded = text.encode("utf-8") + bytes([pad_size]) * pad_size
        openssl = shutil.which("openssl")
        if not openssl:
            raise QQMusicError("网易云登录需要系统 openssl 命令，请先安装 openssl")
        result = subprocess.run(
            [
                openssl,
                "enc",
                "-aes-128-cbc",
                "-K",
                key.encode("utf-8").hex(),
                "-iv",
                b"0102030405060708".hex(),
                "-nosalt",
                "-nopad",
            ],
            input=padded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", "replace").strip()
            raise QQMusicError(f"网易云登录加密失败: {error or 'openssl 执行失败'}")
        return base64.b64encode(result.stdout).decode("ascii")

    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sec_key = "".join(random.choice(chars) for _ in range(16))
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    params = aes_encrypt(aes_encrypt(text, NETEASE_WEAPI_NONCE), sec_key)
    reversed_key = sec_key[::-1].encode("utf-8").hex()
    enc_sec_key = format(
        pow(int(reversed_key, 16), int(NETEASE_WEAPI_PUBKEY, 16), int(NETEASE_WEAPI_MODULUS, 16)),
        "x",
    ).zfill(256)
    return {"params": params, "encSecKey": enc_sec_key}


class QQMusicAPI:
    def __init__(self, base_url: str = DEFAULT_API_BASE, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise QQMusicError("接口返回的不是 JSON") from exc

        code = payload.get("code")
        if code not in (None, 0, 200):
            message = payload.get("message") or payload.get("msg") or "接口返回错误"
            raise QQMusicError(str(message))
        return payload

    def _get_url_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://y.qq.com/portal/profile.html",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
                **(headers or {}),
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise QQMusicError("接口返回的不是 JSON") from exc

    def _request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        method: str | None = None,
        follow_redirects: bool = True,
        timeout: int | None = None,
    ) -> tuple[int, bytes, list[str], str]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
                **(headers or {}),
            },
        )

        opener = urllib.request.build_opener()
        if not follow_redirects:
            opener = urllib.request.build_opener(NoRedirectHandler)

        try:
            with opener.open(request, timeout=timeout or self.timeout) as response:
                body = response.read()
                set_cookies = response.headers.get_all("Set-Cookie") or []
                location = response.headers.get("Location", "")
                return response.status, body, set_cookies, location
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                body = exc.read()
                set_cookies = exc.headers.get_all("Set-Cookie") or []
                location = exc.headers.get("Location", "")
                return exc.code, body, set_cookies, location
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

    def start_qr_login(self) -> QRLoginSession:
        _status, image, set_cookies, _location = self._request(
            "https://ssl.ptlogin2.qq.com/ptqrshow",
            {
                "appid": QQ_QR_APPID,
                "e": 2,
                "l": "M",
                "s": 3,
                "d": 72,
                "v": 4,
                "t": time.time(),
                "daid": 383,
                "pt_3rd_aid": QQ_QR_THIRD_APPID,
                "u1": "https://graph.qq.com/oauth2.0/login_jump",
            },
        )
        cookie = "; ".join(cookie_pairs(set_cookies))
        qrsig = cookie_value(cookie, "qrsig")
        if not qrsig:
            raise QQMusicError("没有拿到二维码会话，请稍后重试")
        return QRLoginSession(qrsig=qrsig, ptqrtoken=hash33(qrsig), image=image)

    def poll_qr_login(self, session: QRLoginSession) -> tuple[str, QRLoginResult | None]:
        cookie_pairs_list = [f"qrsig={session.qrsig}"]
        _status, body, set_cookies, _location = self._request(
            "https://ssl.ptlogin2.qq.com/ptqrlogin",
            {
                "u1": "https://graph.qq.com/oauth2.0/login_jump",
                "ptqrtoken": session.ptqrtoken,
                "ptredirect": 0,
                "h": 1,
                "t": 1,
                "g": 1,
                "from_ui": 1,
                "ptlang": 2052,
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": 23111510,
                "js_type": 1,
                "pt_uistyle": 40,
                "aid": QQ_QR_APPID,
                "daid": 383,
                "pt_3rd_aid": QQ_QR_THIRD_APPID,
                "pt_js_version": "v1.48.1",
            },
            {"Cookie": "; ".join(cookie_pairs_list), "Referer": "https://xui.ptlogin2.qq.com/"},
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        text = body.decode("utf-8", "replace")

        if "二维码已失效" in text or "已失效" in text:
            return "expired", None
        if "二维码未失效" in text or "未扫描" in text:
            return "waiting", None
        if "认证中" in text or "确认" in text:
            return "confirming", None
        if "登录成功" not in text:
            return "waiting", None

        url_match = re.search(r"'(https?://[^']+)'", text)
        if not url_match:
            raise QQMusicError("扫码已确认，但没有拿到授权地址")
        nickname_match = re.search(r"'([^']*)'\s*\)\s*;?\s*$", text)
        nickname = nickname_match.group(1) if nickname_match else ""

        return "done", self.finish_qr_login(url_match.group(1), cookie_pairs_list, nickname)

    def finish_qr_login(self, check_sig_url: str, cookie_pairs_list: list[str], nickname: str = "") -> QRLoginResult:
        _status, _body, set_cookies, _location = self._request(
            check_sig_url,
            {"ptlang": 2052},
            {"Cookie": "; ".join(cookie_pairs_list), "Referer": "https://graph.qq.com/"},
            follow_redirects=False,
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        cookie = "; ".join(cookie_pairs_list)
        p_skey = cookie_value(cookie, "p_skey") or cookie_value(cookie, "skey")
        if not p_skey:
            raise QQMusicError("扫码已确认，但没有拿到 QQ 音乐授权 Cookie")
        gtk = get_gtk(p_skey)

        form = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": QQ_QR_THIRD_APPID,
                "redirect_uri": QQ_QR_REDIRECT_URI,
                "scope": "get_user_info,get_app_friends",
                "state": "state",
                "switch": "",
                "from_ptlogin": 1,
                "src": 1,
                "update_auth": 1,
                "openapi": "1010_1030",
                "g_tk": gtk,
                "auth_time": str(int(time.time())),
                "ui": str(uuid.uuid4()).upper(),
            }
        ).encode("utf-8")
        _status, _body, set_cookies, location = self._request(
            "https://graph.qq.com/oauth2.0/authorize",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "; ".join(cookie_pairs_list),
                "Referer": "https://graph.qq.com/oauth2.0/show",
            },
            data=form,
            method="POST",
            follow_redirects=False,
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if not code_match:
            raise QQMusicError("QQ 授权失败，没有返回登录 code")

        login_payload = json.dumps(
            {
                "comm": {"g_tk": gtk, "platform": "yqq", "ct": 24, "cv": 0},
                "req": {
                    "module": "QQConnectLogin.LoginServer",
                    "method": "QQLogin",
                    "param": {"code": urllib.parse.unquote(code_match.group(1))},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        _status, body, set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "; ".join(cookie_pairs_list),
                "Referer": "https://y.qq.com/",
            },
            data=login_payload,
            method="POST",
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        response = json.loads(body.decode("utf-8", "replace"))
        req_data = response.get("req", {}).get("data", {})
        musicid = str(req_data.get("musicid") or req_data.get("uin") or "")
        musickey = str(req_data.get("musickey") or "")
        if musicid:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"uin=o{musicid}"])
        if musickey:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"qm_keyst={musickey}", f"qqmusic_key={musickey}"])

        cookie = "; ".join(cookie_pairs_list)
        qq_number = musicid or qq_number_from_cookie(cookie)
        if not qq_number:
            raise QQMusicError("登录成功但没有识别到 QQ 号")
        return QRLoginResult(qq_number=qq_number, cookie=cookie, nickname=nickname)

    def start_wx_qr_login(self) -> WXLoginSession:
        params = {
            "appid": WX_QR_APPID,
            "redirect_uri": WX_QR_REDIRECT_URI,
            "response_type": "code",
            "scope": "snsapi_login",
            "state": "STATE",
            "href": "https://y.qq.com/mediastyle/music_v17/src/css/popup_wechat.css#wechat_redirect",
        }
        url = f"https://open.weixin.qq.com/connect/qrconnect?{urllib.parse.urlencode(params)}"
        _status, body, _set_cookies, _location = self._request(
            url,
            headers={"Referer": "https://y.qq.com/"},
        )
        html = body.decode("utf-8", "replace")
        uuid_match = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", html)
        if not uuid_match:
            raise QQMusicError("没有拿到微信登录二维码，请稍后重试")
        wx_uuid = uuid_match.group(1)
        _status, image, _set_cookies, _location = self._request(
            f"https://open.weixin.qq.com/connect/qrcode/{wx_uuid}",
            headers={"Referer": url},
        )
        return WXLoginSession(uuid=wx_uuid, image=image, referer=url)

    def poll_wx_qr_login(self, session: WXLoginSession) -> tuple[str, QRLoginResult | None]:
        params = {"uuid": session.uuid}
        if session.last_code:
            params["last"] = session.last_code
        _status, body, _set_cookies, _location = self._request(
            "https://long.open.weixin.qq.com/connect/l/qrconnect",
            params,
            {"Referer": session.referer},
            timeout=35,
        )
        text = body.decode("utf-8", "replace")
        match = re.search(r"window\.wx_errcode=(\d+);window\.wx_code='([^']*)'", text)
        if not match:
            return "waiting", None
        errcode, code = match.group(1), match.group(2)
        session.last_code = errcode
        if errcode == "405" and code:
            return "done", self.finish_wx_qr_login(code)
        if errcode == "404":
            return "confirming", None
        if errcode == "402":
            return "expired", None
        if errcode == "403":
            return "cancelled", None
        return "waiting", None

    def finish_wx_qr_login(self, code: str) -> QRLoginResult:
        payload = json.dumps(
            {
                "comm": {"tmeAppID": "qqmusic", "tmeLoginType": "1", "platform": "yqq", "ct": 24, "cv": 0},
                "req": {
                    "module": "music.login.LoginServer",
                    "method": "Login",
                    "param": {"strAppid": WX_QR_APPID, "code": code},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _status, body, set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": WX_QR_REDIRECT_URI,
            },
            data=payload,
            method="POST",
        )
        response = parse_json_like_response(body)
        if response.get("code") not in (None, 0):
            raise QQMusicError(str(response.get("message") or response.get("msg") or "微信登录失败"))
        req = response.get("req") if isinstance(response.get("req"), dict) else {}
        if req.get("code") not in (None, 0):
            raise QQMusicError(str(req.get("message") or req.get("msg") or "微信登录失败"))
        req_data = req.get("data") if isinstance(req.get("data"), dict) else {}

        cookie_pairs_list = merge_cookie_pairs([], cookie_pairs(set_cookies))
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, ["login_type=2", "tmeLoginType=1"])
        wxuin = str(req_data.get("wxuin") or req_data.get("uin") or req_data.get("musicid") or "")
        musicid = str(req_data.get("musicid") or req_data.get("uin") or "")
        musickey = str(
            req_data.get("musickey")
            or req_data.get("music_key")
            or req_data.get("qm_keyst")
            or req_data.get("qqmusic_key")
            or ""
        )
        if wxuin:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"wxuin={wxuin}"])
        if musicid:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"uin=o{musicid}"])
        if musickey:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"qm_keyst={musickey}", f"qqmusic_key={musickey}"])

        cookie = "; ".join(cookie_pairs_list)
        qq_number = qq_number_from_cookie(cookie)
        if not qq_number:
            raise QQMusicError("微信扫码已确认，但没有识别到 QQ 音乐账号 ID")
        return QRLoginResult(qq_number=qq_number, cookie=cookie, nickname="微信登录")

    def search(self, keyword: str, count: int = 30, page: int = 1, cookie: str = "") -> list[Song]:
        if not cookie:
            raise QQMusicError("QQ 音乐搜索现在需要登录，请先登录后再搜索")
        uin = qq_number_from_cookie(cookie) or "0"
        request_data = {
            "comm": {
                "ct": 24,
                "cv": 0,
                "uin": uin,
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 1,
            },
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "remoteplace": "yqq.yqq.yqq",
                    "searchid": str(random.randint(10**17, 10**18 - 1)),
                    "search_type": 0,
                    "query": keyword,
                    "page_num": page,
                    "num_per_page": count,
                },
            },
        }
        _status, body, _set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Cookie": cookie,
                "Referer": "https://y.qq.com/n/ryqq/search",
                "Content-Type": "application/json",
            },
            data=json.dumps(request_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
        )
        payload = parse_json_like_response(body)
        code = payload.get("code")
        req = payload.get("req_1") if isinstance(payload.get("req_1"), dict) else {}
        req_code = req.get("code")
        if code not in (None, 0) or req_code not in (None, 0):
            message = req.get("message") or req.get("msg") or payload.get("message") or payload.get("msg") or "搜索接口返回错误"
            raise QQMusicError(str(message))
        data = req.get("data") if isinstance(req.get("data"), dict) else {}
        body_data = data.get("body") if isinstance(data.get("body"), dict) else {}
        song_data = body_data.get("song") if isinstance(body_data.get("song"), dict) else {}
        items = song_data.get("list") if isinstance(song_data, dict) else []
        if not isinstance(items, list):
            items = []
        return [normalize_song(item) for item in items if isinstance(item, dict)]

    def song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        try:
            qualities = [quality, "320", "128"] if quality == "flac" else [quality, "128"]
            tried: set[str] = set()
            last_error: QQMusicError | None = None
            for candidate in qualities:
                if candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    return self.qq_song_url(mid, candidate, cookie, media_mid)
                except QQMusicError as exc:
                    last_error = exc
            if media_mid and media_mid != mid:
                for candidate in qualities:
                    if candidate in tried and len(tried) > len(qualities):
                        continue
                    try:
                        return self.qq_song_url(mid, candidate, cookie, mid)
                    except QQMusicError as exc:
                        last_error = exc
            raise last_error or QQMusicError("QQ 音乐没有返回可播放链接")
        except QQMusicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QQMusicError(f"获取播放链接失败: {exc}") from exc

    def qq_song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        file_types = {
            "128": ("M500", ".mp3"),
            "320": ("M800", ".mp3"),
            "flac": ("F000", ".flac"),
        }
        prefix, suffix = file_types.get(quality, file_types["320"])
        filename = f"{prefix}{media_mid or mid}{suffix}"
        uin = qq_number_from_cookie(cookie) or "0"
        guid = str(random.randint(1000000000, 9999999999))
        request_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": [filename],
                    "guid": guid,
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0},
        }
        payload = self._get_url_json(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "data": json.dumps(request_data, ensure_ascii=False, separators=(",", ":")),
            },
            {"Cookie": cookie, "Referer": "https://y.qq.com/"} if cookie else {"Referer": "https://y.qq.com/"},
        )
        data = payload.get("req_0", {}).get("data", {})
        midurlinfo = data.get("midurlinfo") or []
        purl = ""
        if midurlinfo and isinstance(midurlinfo[0], dict):
            purl = midurlinfo[0].get("purl") or midurlinfo[0].get("wifiurl") or ""
        if not purl:
            raise QQMusicError("QQ 音乐没有返回可播放链接，可能是会员、版权、海外地区、音质限制，或当前登录方式权限不足")
        if purl.startswith("http"):
            return purl
        sip = data.get("sip") or []
        domain = next((item for item in sip if isinstance(item, str) and not item.startswith("http://ws")), "")
        if not domain and sip:
            domain = str(sip[0])
        if not domain:
            domain = "https://dl.stream.qqmusic.qq.com/"
        return urllib.parse.urljoin(domain, purl)

    def lyric(self, mid: str, cookie: str = "") -> str:
        return self.qq_lyric(mid, cookie)

    def qq_lyric(self, mid: str, cookie: str = "") -> str:
        uin = qq_number_from_cookie(cookie) or "0"
        payload = self._get_url_json(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            {
                "songmid": mid,
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "nobase64": 1,
            },
            {"Cookie": cookie, "Referer": "https://y.qq.com/"} if cookie else {"Referer": "https://y.qq.com/"},
        )
        lyric = str(payload.get("lyric") or "")
        trans = str(payload.get("trans") or "")
        if lyric and not lyric.lstrip().startswith("["):
            lyric = decode_possible_base64(lyric)
        if trans and not trans.lstrip().startswith("["):
            trans = decode_possible_base64(trans)
        if trans:
            return f"{lyric}\n\n--- 翻译 ---\n{trans}".strip()
        return lyric.strip()

    def playlist_songs_page(
        self,
        playlist_id: str,
        cookie: str = "",
        begin: int = 0,
        count: int = PLAYLIST_BACKGROUND_PAGE_SIZE,
    ) -> tuple[str, list[Song], int]:
        payload = self._get_url_json(
            "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
            {
                "disstid": playlist_id,
                "type": 1,
                "json": 1,
                "utf8": 1,
                "onlysong": 0,
                "new_format": 1,
                "g_tk": 5381,
                "loginUin": 0,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "song_begin": max(0, begin),
                "song_num": max(1, count),
            },
            {
                "Cookie": cookie,
                "Referer": f"https://y.qq.com/n/ryqq/playlist/{playlist_id}",
            }
            if cookie
            else {"Referer": f"https://y.qq.com/n/ryqq/playlist/{playlist_id}"},
        )

        code = payload.get("code")
        if code not in (None, 0):
            message = payload.get("message") or payload.get("msg") or "歌单详情接口返回错误"
            raise QQMusicError(str(message))

        cdlist = payload.get("cdlist") or []
        data = cdlist[0] if isinstance(cdlist, list) and cdlist else payload.get("data") or {}
        if not isinstance(data, dict):
            raise QQMusicError("歌单详情接口返回格式异常")

        name = str(data.get("dissname") or data.get("name") or f"歌单 {playlist_id}")
        items = data.get("songlist") or data.get("songs") or []
        if not isinstance(items, list):
            items = []
        total_value = data.get("total_song_num") or data.get("songnum") or data.get("total") or data.get("song_count")
        try:
            total = int(total_value) if total_value is not None else begin + len(items)
        except (TypeError, ValueError):
            total = begin + len(items)
        songs = [normalize_song(item) for item in items if isinstance(item, dict)]
        return name, songs, max(total, begin + len(songs))

    def playlist_songs(self, playlist_id: str, cookie: str = "") -> tuple[str, list[Song]]:
        name, results, total = self.playlist_songs_page(playlist_id, cookie, 0, PLAYLIST_BACKGROUND_PAGE_SIZE)
        loaded = len(results)
        while True:
            page_name, page_results, page_total = self.playlist_songs_page(playlist_id, cookie, loaded, PLAYLIST_BACKGROUND_PAGE_SIZE)
            if page_name:
                name = page_name
            if page_total > total:
                total = page_total
            if not page_results:
                break
            results.extend(page_results)
            loaded += len(page_results)
            if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE:
                break
            if loaded >= total:
                total = loaded + PLAYLIST_BACKGROUND_PAGE_SIZE
        return name, results

    def user_playlists(self, qq_number: str, cookie: str = "") -> list[Playlist]:
        payload = self._get_url_json(
            "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss",
            {
                "hostUin": 0,
                "hostuin": qq_number,
                "sin": 0,
                "size": 200,
                "g_tk": 5381,
                "loginUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
            },
            {"Cookie": cookie} if cookie else None,
        )

        code = payload.get("code")
        if code == 4000:
            raise QQMusicError("这个账号没有公开歌单，或当前 Cookie 没有权限读取")
        if code == 1000:
            raise QQMusicError("登录已失效，请重新导入 QQ 音乐 Cookie")

        data = payload.get("data") or {}
        items = data.get("disslist") or []
        if not isinstance(items, list):
            raise QQMusicError("用户歌单接口返回格式异常")

        playlists = [normalize_playlist(item) for item in items if isinstance(item, dict)]
        return [playlist for playlist in playlists if playlist.id]

    def user_nickname(self, qq_number: str, cookie: str = "") -> str:
        if not qq_number:
            return ""
        payload = self._get_url_json(
            "https://c.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg",
            {
                "cid": 205360838,
                "userid": qq_number,
                "reqfrom": 1,
                "g_tk": 5381,
                "loginUin": qq_number,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
            },
            {"Cookie": cookie} if cookie else None,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        creator = data.get("creator") if isinstance(data.get("creator"), dict) else {}
        return str(creator.get("nick") or creator.get("nickname") or "").strip()

    def add_song_to_playlist(
        self,
        song_id: str,
        song_type: int,
        playlist_dirid: str,
        cookie: str,
        song_mid: str = "",
        playlist_id: str = "",
    ) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        if not song_id:
            raise QQMusicError("当前歌曲缺少 QQ 音乐 song id")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistDetailWrite",
                    "method": "AddSonglist",
                    "param": {
                        "dirId": dir_id,
                        "v_songInfo": [
                            {
                                "songType": song_type,
                                "songId": int(song_id) if str(song_id).isdigit() else song_id,
                            }
                        ],
                    },
                }
            },
            cookie,
            "https://y.qq.com/n/ryqq/playlist",
            include_comm=False,
        )
        tried_legacy = False
        try:
            self._raise_if_musicu_failed(payload, "req_0", "添加歌曲")
        except QQMusicError as exc:
            if not song_mid:
                raise
            try:
                self.add_song_to_playlist_legacy(song_mid, song_type, playlist_dirid, cookie)
                tried_legacy = True
            except QQMusicError:
                raise exc
        if playlist_id and not self._playlist_contains_song_after_add(playlist_id, song_id, song_mid, cookie):
            if song_mid and not tried_legacy:
                self.add_song_to_playlist_legacy(song_mid, song_type, playlist_dirid, cookie)
                if self._playlist_contains_song_after_add(playlist_id, song_id, song_mid, cookie):
                    return
            raise QQMusicError("QQ 音乐接口返回成功，但刷新目标歌单后没有发现这首歌。请重新登录后再试。")

    def add_song_to_playlist_legacy(self, song_mid: str, song_type: int, playlist_dirid: str, cookie: str) -> None:
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/fcg_music_add2songdir.fcg",
            {
                "g_tk": gtk_from_cookie(cookie),
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.post",
                "needNewCode": 0,
                "uin": uin,
                "dirid": playlist_dirid,
                "midlist": song_mid,
                "typelist": song_type,
                "addtype": "",
                "formsender": 4,
                "source": 103,
                "utf8": 1,
            },
            cookie,
            "https://y.qq.com/n/ryqq/playlist",
        )
        self._raise_if_write_failed(payload, "添加歌曲")

    def _playlist_contains_song_after_add(self, playlist_id: str, song_id: str, song_mid: str, cookie: str) -> bool:
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            _name, songs = self.playlist_songs(playlist_id, cookie)
            for song in songs:
                if song_id and song.song_id == song_id:
                    return True
                if song_mid and song.mid == song_mid:
                    return True
        return False

    def remove_song_from_playlist(self, song_id: str, song_type: int, playlist_dirid: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        if not song_id:
            raise QQMusicError("当前歌曲缺少 QQ 音乐 song id")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistDetailWrite",
                    "method": "DelSonglist",
                    "param": {
                        "dirId": dir_id,
                        "v_songInfo": [
                            {
                                "songType": song_type,
                                "songId": int(song_id) if str(song_id).isdigit() else song_id,
                            }
                        ],
                    },
                }
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
            include_comm=False,
        )
        self._raise_if_musicu_failed(payload, "req_0", "移除歌曲")

    def create_playlist(self, name: str, cookie: str) -> str:
        if not cookie:
            raise QQMusicError("请先登录")
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/create_playlist.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf8",
                "notice": 0,
                "platform": "yqq",
                "needNewCode": 0,
                "uin": uin,
                "name": name,
                "show": 1,
                "formsender": 1,
                "utf8": 1,
                "qzreferrer": "https://y.qq.com/portal/profile.html#sub=other&tab=create&",
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
        )
        self._raise_if_write_failed(payload, "创建歌单")
        return str(payload.get("dirid") or "")

    def delete_playlist(self, playlist_dirid: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/fcg_fav_modsongdir.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "GB2312",
                "outCharset": "gb2312",
                "notice": 0,
                "platform": "yqq",
                "needNewCode": 0,
                "uin": uin,
                "delnum": 1,
                "deldirids": playlist_dirid,
                "forcedel": 1,
                "formsender": 1,
                "source": 103,
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
            "gb18030",
        )
        self._raise_if_write_failed(payload, "删除歌单")

    def rename_playlist(self, playlist_dirid: str, new_name: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistBaseWrite",
                    "method": "EditPlaylist",
                    "param": {
                        "dirId": dir_id,
                        "mask": 1,
                        "dirNewName": new_name,
                    },
                }
            },
            cookie,
            f"https://y.qq.com/n/ryqq/playlist_edit/{playlist_dirid}",
            include_comm=False,
        )
        self._raise_if_musicu_failed(payload, "req_0", "重命名歌单")

    def _post_form_json(self, url: str, params: dict[str, Any], cookie: str, referer: str, encoding: str = "utf-8") -> dict[str, Any]:
        clean_params = {key: value for key, value in params.items() if value is not None}
        body = urllib.parse.urlencode(clean_params, encoding=encoding).encode(encoding, "replace")
        _status, response_body, _set_cookies, _location = self._request(
            url,
            headers={
                "Cookie": cookie,
                "Referer": referer,
                "Content-Type": f"application/x-www-form-urlencoded; charset={encoding}",
            },
            data=body,
            method="POST",
        )
        payload = parse_json_like_response(response_body)
        if not isinstance(payload, dict):
            raise QQMusicError("接口返回格式异常")
        return payload

    def _post_musicu_json(
        self,
        requests: dict[str, Any],
        cookie: str,
        referer: str = "https://y.qq.com/",
        include_comm: bool = True,
    ) -> dict[str, Any]:
        uin = qq_number_from_cookie(cookie)
        comm = {
            "g_tk": gtk_from_cookie(cookie),
            "uin": uin,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": 0,
            "platform": "yqq.json",
            "needNewCode": 1,
        }
        comm.update(musicu_login_fields(cookie))
        payload = {"comm": comm, **requests} if include_comm else dict(requests)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        _status, response_body, _set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Cookie": cookie,
                "Referer": referer,
                "Content-Type": "application/json",
            },
            data=body,
            method="POST",
        )
        result = parse_json_like_response(response_body)
        if not isinstance(result, dict):
            raise QQMusicError("接口返回格式异常")
        return result

    def _raise_if_write_failed(self, payload: dict[str, Any], action: str) -> None:
        failure_code: Any = None
        for key in ("code", "subcode", "retcode", "retCode", "errcode"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                is_success = int(value) == 0
            except (TypeError, ValueError):
                is_success = str(value).lower() in {"ok", "success"}
            if not is_success:
                failure_code = value
                break
        if failure_code is None:
            return
        message = payload.get("msg") or payload.get("message") or payload.get("errMsg") or f"{action}失败（错误码: {failure_code}）"
        if str(failure_code) == "1000" or (str(failure_code) == "1" and message == f"{action}失败"):
            raise QQMusicError("登录已失效，请重新登录")
        raise QQMusicError(str(message))

    def _raise_if_musicu_failed(self, payload: dict[str, Any], request_key: str, action: str) -> None:
        self._raise_if_write_failed(payload, action)
        request_payload = payload.get(request_key)
        if not isinstance(request_payload, dict):
            raise QQMusicError(f"{action}失败")
        self._raise_if_write_failed(request_payload, action)
        data = request_payload.get("data")
        if not isinstance(data, dict):
            return
        self._raise_if_write_failed(data, action)
        retcode = data.get("retCode")
        if retcode in (None, 0):
            return
        message = data.get("msg") or data.get("message") or data.get("errMsg") or f"{action}失败（错误码: {retcode}）"
        if str(retcode) == "1000":
            raise QQMusicError("登录已失效，请重新登录")
        raise QQMusicError(str(message))


class NeteaseMusicAPI:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.session_cookie_pairs: list[str] = []
        self.device_id = "".join(random.choice("0123456789ABCDEF") for _ in range(52))
        self._ntes_nuid = binascii.hexlify(os.urandom(32)).decode("ascii")
        self.wnmcid = "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6)) + f".{int(time.time() * 1000)}.01.0"
        self.anonymous_ready = False

    def _base_cookie_pairs(self) -> list[str]:
        return [
            "__remember_me=true",
            "ntes_kaola_ad=1",
            f"_ntes_nuid={self._ntes_nuid}",
            f"_ntes_nnid={self._ntes_nuid},{int(time.time() * 1000)}",
            f"WNMCID={self.wnmcid}",
            "WEVNSM=1.0.0",
            "osver=Microsoft-Windows-10-Professional-build-19045-64bit",
            f"deviceId={self.device_id}",
            "os=pc",
            "channel=netease",
            "appver=3.1.17.204416",
        ]

    def _merged_cookie_header(self, explicit_cookie: str = "") -> str:
        explicit_pairs = [pair.strip() for pair in explicit_cookie.split(";") if "=" in pair]
        merged = merge_cookie_pairs(self._base_cookie_pairs(), self.session_cookie_pairs)
        merged = merge_cookie_pairs(merged, explicit_pairs)
        has_music_u = any(pair.split("=", 1)[0] == "MUSIC_U" for pair in merged)
        has_music_a = any(pair.split("=", 1)[0] == "MUSIC_A" for pair in merged)
        if not has_music_u and not has_music_a:
            # The anonymous registration call will fill MUSIC_A. Keep the
            # rest of the web client cookies stable before that happens.
            pass
        return "; ".join(merged)

    def _cloudmusic_dll_encode_id(self, value: str) -> str:
        xored = "".join(
            chr(ord(char) ^ ord(NETEASE_DEVICE_XOR_KEY[index % len(NETEASE_DEVICE_XOR_KEY)]))
            for index, char in enumerate(value)
        )
        digest = hashlib.md5(xored.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def ensure_anonymous_session(self) -> None:
        if self.anonymous_ready or any(pair.startswith("MUSIC_A=") for pair in self.session_cookie_pairs):
            self.anonymous_ready = True
            return
        encoded_id = base64.b64encode(f"{self.device_id} {self._cloudmusic_dll_encode_id(self.device_id)}".encode("utf-8")).decode("ascii")
        try:
            payload, set_cookies = self._post_form_json(
                "/weapi/register/anonimous",
                {"username": encoded_id},
                encrypted=True,
                include_session_cookie=False,
            )
        except QQMusicError:
            return
        if payload.get("code") == 200 and set_cookies:
            self.session_cookie_pairs = merge_cookie_pairs(self.session_cookie_pairs, cookie_pairs(set_cookies))
            self.anonymous_ready = True

    def _request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
                "User-Agent": NETEASE_WEB_UA,
                **(headers or {}),
        }
        request_headers["Cookie"] = self._merged_cookie_header(request_headers.get("Cookie", ""))

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                set_cookies = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            body = exc.read()
            set_cookies = exc.headers.get_all("Set-Cookie") or []
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc
        payload = parse_json_like_response(body)
        if not isinstance(payload, dict):
            raise QQMusicError("网易云接口返回格式异常")
        if set_cookies:
            self.session_cookie_pairs = merge_cookie_pairs(self.session_cookie_pairs, cookie_pairs(set_cookies))
        return payload, set_cookies

    def _get_json(self, path: str, params: dict[str, Any] | None = None, cookie: str = "") -> dict[str, Any]:
        headers = {"Cookie": cookie} if cookie else None
        payload, _set_cookies = self._request(f"{NETEASE_BASE_URL}{path}", params=params, headers=headers)
        return payload

    def _post_form_json(
        self,
        path: str,
        params: dict[str, Any],
        cookie: str = "",
        encrypted: bool = False,
        include_session_cookie: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        body_params = netease_weapi_encrypt(params) if encrypted else params
        body = urllib.parse.urlencode(body_params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        elif not include_session_cookie:
            headers["Cookie"] = "; ".join(self._base_cookie_pairs())
        return self._request(f"{NETEASE_BASE_URL}{path}", headers=headers, data=body, method="POST")

    def _post_weapi_json(self, path: str, params: dict[str, Any], cookie: str, action: str) -> dict[str, Any]:
        if not cookie:
            raise QQMusicError("请先登录")
        csrf_token = cookie_value(cookie, "__csrf")
        request_path = path
        if csrf_token:
            separator = "&" if "?" in request_path else "?"
            request_path = f"{request_path}{separator}csrf_token={urllib.parse.quote(csrf_token)}"
        payload_params = dict(params)
        payload_params.setdefault("csrf_token", csrf_token)
        payload, _cookies = self._post_form_json(request_path, payload_params, cookie, encrypted=True)
        self._raise_if_failed(payload, action)
        return payload

    def _raise_if_failed(self, payload: dict[str, Any], action: str) -> None:
        code = payload.get("code")
        if code in (None, 200, 201):
            return
        message = payload.get("message") or payload.get("msg") or payload.get("errmsg") or f"{action}失败（错误码: {code}）"
        if str(code) in {"301", "-462"}:
            raise QQMusicError("网易云登录已失效，请重新登录")
        raise QQMusicError(str(message))

    def send_phone_captcha(self, phone: str, country_code: str = "86") -> None:
        self.ensure_anonymous_session()
        payload, _cookies = self._post_form_json(
            "/weapi/sms/captcha/sent",
            {
                "cellphone": phone,
                "ctcode": country_code,
            },
            encrypted=True,
        )
        if payload.get("code") != 200 or payload.get("data") is False:
            payload, _cookies = self._post_form_json(
                "/weapi/sms/captcha/sent",
                {
                    "cellphone": phone,
                    "ctcode": country_code,
                    "secrete": "music_middleuser_pclogin",
                },
                encrypted=True,
            )
        if payload.get("code") != 200 or payload.get("data") is False:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "发送验证码失败"))

    def verify_phone_captcha(self, phone: str, captcha: str, country_code: str = "86") -> None:
        self.ensure_anonymous_session()
        payload, _cookies = self._post_form_json(
            "/weapi/sms/captcha/verify",
            {
                "cellphone": phone,
                "captcha": captcha,
                "ctcode": country_code,
            },
            encrypted=True,
        )
        if payload.get("code") != 200 or payload.get("data") is False:
            message = payload.get("message") or payload.get("msg") or "验证码校验失败"
            code = payload.get("code")
            raise QQMusicError(f"{message}（错误码: {code}）" if code is not None else str(message))

    def login_with_phone_captcha(self, phone: str, captcha: str, country_code: str = "86") -> QRLoginResult:
        self.ensure_anonymous_session()
        payload, set_cookies = self._login_with_phone_captcha_once(phone, captcha, country_code)
        if str(payload.get("code")) == "10004":
            self.verify_phone_captcha(phone, captcha, country_code)
            payload, set_cookies = self._login_with_phone_captcha_once(phone, captcha, country_code)
        if payload.get("code") != 200:
            message = payload.get("message") or payload.get("msg") or "网易云登录失败"
            code = payload.get("code")
            if str(code) == "10004":
                message = f"{message}。这是网易云服务端风控，请稍后再试，或先用官方网页登录后导入 Cookie。"
            raise QQMusicError(f"{message}（错误码: {code}）" if code is not None else str(message))
        return self._login_result_from_payload(payload, set_cookies)

    def _login_with_phone_captcha_once(self, phone: str, captcha: str, country_code: str) -> tuple[dict[str, Any], list[str]]:
        payload, set_cookies = self._post_form_json(
            "/weapi/w/login/cellphone",
            {
                "type": "1",
                "https": "true",
                "phone": phone,
                "countrycode": country_code,
                "captcha": captcha,
                "remember": "true",
            },
            encrypted=True,
        )
        return payload, set_cookies

    def _login_result_from_payload(self, payload: dict[str, Any], set_cookies: list[str]) -> QRLoginResult:
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        user_id = str(profile.get("userId") or account.get("id") or "")
        nickname = str(profile.get("nickname") or "")
        cookie_pairs_list = cookie_pairs(set_cookies)
        cookie_text = payload.get("cookie")
        if isinstance(cookie_text, str) and cookie_text:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [pair.strip() for pair in cookie_text.split(";") if "=" in pair])
        cookie = "; ".join(cookie_pairs_list)
        if not user_id:
            raise QQMusicError("登录成功但没有识别到网易云用户 ID")
        if not cookie:
            raise QQMusicError("登录成功但没有拿到网易云 Cookie")
        return QRLoginResult(qq_number=user_id, cookie=cookie, nickname=nickname)

    def login_with_cookie(self, cookie: str) -> QRLoginResult:
        cookie = cookie.strip()
        if not cookie:
            raise QQMusicError("Cookie 不能为空")
        payload = self._get_json("/api/nuser/account/get", cookie=cookie)
        if payload.get("code") != 200:
            message = payload.get("message") or payload.get("msg") or "网易云 Cookie 校验失败"
            raise QQMusicError(str(message))
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        user_id = str(profile.get("userId") or account.get("id") or "")
        nickname = str(profile.get("nickname") or "")
        if not user_id:
            raise QQMusicError("这个 Cookie 没有有效网易云登录态，请确认包含 MUSIC_U")
        return QRLoginResult(qq_number=user_id, cookie=cookie, nickname=nickname)

    def search(self, keyword: str, count: int = 30, page: int = 1, cookie: str = "") -> list[Song]:
        payload, _cookies = self._post_form_json(
            "/api/cloudsearch/pc",
            {
                "s": keyword,
                "type": 1,
                "offset": max(0, page - 1) * count,
                "total": "true" if page == 1 else "false",
                "limit": count,
            },
            cookie,
        )
        if payload.get("code") not in (None, 200):
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "搜索接口返回错误"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        items = result.get("songs") if isinstance(result.get("songs"), list) else []
        return [normalize_netease_song(item) for item in items if isinstance(item, dict)]

    def song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        bitrates = {"128": 128000, "320": 320000, "flac": 999000}
        levels = {
            "128": ("standard", "128"),
            "320": ("exhigh", "standard", "128"),
            "flac": ("lossless", "exhigh", "standard", "128"),
        }
        song_id = media_mid or mid
        normalized_id = int(song_id) if str(song_id).isdigit() else song_id
        last_message = ""
        for level in levels.get(quality, levels["320"]):
            payload = self._get_json(
                "/api/song/enhance/player/url/v1",
                {"ids": json.dumps([normalized_id]), "level": level, "encodeType": "mp3"},
                cookie,
            )
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            item = data[0] if data and isinstance(data[0], dict) else {}
            url = str(item.get("url") or "")
            last_message = str(item.get("message") or payload.get("message") or payload.get("msg") or last_message or "")
            if url and self._stream_url_available(url):
                return url
        for br in (bitrates.get(quality, 320000), 320000, 128000):
            payload = self._get_json(
                "/api/song/enhance/player/url",
                {"ids": json.dumps([normalized_id]), "br": br},
                cookie,
            )
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            item = data[0] if data and isinstance(data[0], dict) else {}
            url = str(item.get("url") or "")
            last_message = str(item.get("message") or payload.get("message") or payload.get("msg") or last_message or "")
            if url and self._stream_url_available(url):
                return url
        if last_message:
            raise QQMusicError(f"网易云没有返回可用播放链接：{last_message}")
        raise QQMusicError("网易云没有返回可播放链接，可能是会员、版权、地区或登录限制")

    def _stream_url_available(self, url: str) -> bool:
        headers = {
            "User-Agent": NETEASE_WEB_UA,
            "Referer": "https://music.163.com/",
            "Range": "bytes=0-0",
        }
        for method in ("HEAD", "GET"):
            try:
                request = urllib.request.Request(url, method=method, headers=headers)
                with urllib.request.urlopen(request, timeout=min(5, self.timeout)) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if 200 <= response.status < 400 and (not content_type or "text/html" not in content_type.lower()):
                        return True
            except urllib.error.HTTPError as exc:
                if method == "HEAD" and exc.code in {403, 405, 501}:
                    continue
                return False
            except (urllib.error.URLError, TimeoutError, OSError):
                return False
        return False

    def lyric(self, mid: str, cookie: str = "") -> str:
        payload = self._get_json(
            "/api/song/lyric",
            {"os": "pc", "id": mid, "lv": -1, "kv": -1, "tv": -1},
            cookie,
        )
        lrc = payload.get("lrc") if isinstance(payload.get("lrc"), dict) else {}
        trans = payload.get("tlyric") if isinstance(payload.get("tlyric"), dict) else {}
        lyric = str(lrc.get("lyric") or "")
        translated = str(trans.get("lyric") or "")
        if translated:
            return f"{lyric}\n\n--- 翻译 ---\n{translated}".strip()
        return lyric.strip()

    def playlist_songs_page(
        self,
        playlist_id: str,
        cookie: str = "",
        begin: int = 0,
        count: int = PLAYLIST_BACKGROUND_PAGE_SIZE,
    ) -> tuple[str, list[Song], int]:
        fetch_count = max(1, begin + count)
        payload = self._get_json(
            "/api/v6/playlist/detail",
            {"id": playlist_id, "n": fetch_count, "s": 0},
            cookie,
        )
        if payload.get("code") != 200:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "歌单详情接口返回错误"))
        playlist = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        name = str(playlist.get("name") or f"歌单 {playlist_id}")
        tracks = playlist.get("tracks") if isinstance(playlist.get("tracks"), list) else []
        total = playlist.get("trackCount") or len(playlist.get("trackIds") or []) or len(tracks)
        try:
            total_count = int(total)
        except (TypeError, ValueError):
            total_count = len(tracks)
        songs = [normalize_netease_song(item) for item in tracks[begin : begin + count] if isinstance(item, dict)]
        return name, songs, max(total_count, begin + len(songs))

    def playlist_songs(self, playlist_id: str, cookie: str = "") -> tuple[str, list[Song]]:
        name, results, total = self.playlist_songs_page(playlist_id, cookie, 0, PLAYLIST_BACKGROUND_PAGE_SIZE)
        loaded = len(results)
        while True:
            page_name, page_results, page_total = self.playlist_songs_page(playlist_id, cookie, loaded, PLAYLIST_BACKGROUND_PAGE_SIZE)
            if page_name:
                name = page_name
            if page_total > total:
                total = page_total
            if not page_results:
                break
            results.extend(page_results)
            loaded += len(page_results)
            if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE:
                break
            if loaded >= total:
                total = loaded + PLAYLIST_BACKGROUND_PAGE_SIZE
        return name, results

    def user_playlists(self, user_id: str, cookie: str = "") -> list[Playlist]:
        payload = self._get_json(
            "/api/user/playlist/",
            {"uid": user_id, "offset": 0, "limit": 1000},
            cookie,
        )
        if payload.get("code") != 200:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "用户歌单接口返回错误"))
        items = payload.get("playlist") if isinstance(payload.get("playlist"), list) else []
        return [normalize_netease_playlist(item) for item in items if isinstance(item, dict)]

    def add_song_to_playlist(
        self,
        song_id: str,
        _song_type: int,
        playlist_dirid: str,
        cookie: str,
        _song_mid: str = "",
        _playlist_id: str = "",
    ) -> None:
        if not song_id:
            raise QQMusicError("当前歌曲缺少网易云歌曲 ID")
        if not playlist_dirid:
            raise QQMusicError("目标歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/manipulate/tracks",
            {
                "op": "add",
                "pid": playlist_dirid,
                "trackIds": json.dumps([int(song_id) if str(song_id).isdigit() else song_id]),
                "imme": "true",
            },
            cookie,
            "添加歌曲",
        )

    def remove_song_from_playlist(self, song_id: str, _song_type: int, playlist_dirid: str, cookie: str) -> None:
        if not song_id:
            raise QQMusicError("当前歌曲缺少网易云歌曲 ID")
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/manipulate/tracks",
            {
                "op": "del",
                "pid": playlist_dirid,
                "trackIds": json.dumps([int(song_id) if str(song_id).isdigit() else song_id]),
                "imme": "true",
            },
            cookie,
            "移除歌曲",
        )

    def create_playlist(self, name: str, cookie: str) -> str:
        payload = self._post_weapi_json(
            "/weapi/playlist/create",
            {
                "name": name,
                "privacy": 0,
                "type": "NORMAL",
            },
            cookie,
            "创建歌单",
        )
        playlist = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        return str(playlist.get("id") or payload.get("id") or "")

    def delete_playlist(self, playlist_dirid: str, cookie: str) -> None:
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/remove",
            {"ids": f"[{playlist_dirid}]"},
            cookie,
            "删除歌单",
        )

    def rename_playlist(self, playlist_dirid: str, new_name: str, cookie: str) -> None:
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/update/name",
            {
                "id": playlist_dirid,
                "name": new_name,
            },
            cookie,
            "重命名歌单",
        )


def normalize_song(item: dict[str, Any]) -> Song:
    title = str(item.get("title") or item.get("songname") or item.get("name") or "未知歌曲")
    mid = str(item.get("mid") or item.get("songmid") or item.get("strMediaMid") or "")
    song_id = str(item.get("id") or item.get("songid") or "")
    try:
        song_type = int(item.get("type") if item.get("type") is not None else item.get("songtype") or 0)
    except (TypeError, ValueError):
        song_type = 0
    file_value = item.get("file") if isinstance(item.get("file"), dict) else {}
    media_mid = str(
        item.get("media_mid")
        or item.get("strMediaMid")
        or item.get("mediaMid")
        or file_value.get("media_mid")
        or file_value.get("strMediaMid")
        or mid
    )

    singers_value = item.get("singer") or item.get("singers") or []
    if isinstance(singers_value, list):
        singers = "/".join(str(s.get("name", "")) for s in singers_value if isinstance(s, dict))
    elif isinstance(singers_value, dict):
        singers = str(singers_value.get("name") or "")
    else:
        singers = str(singers_value or "")

    album_value = item.get("album") or item.get("albumname") or ""
    if isinstance(album_value, dict):
        album = str(album_value.get("name") or album_value.get("title") or "")
    else:
        album = str(album_value or "")

    duration = item.get("interval") or item.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    return Song(
        title=title,
        mid=mid,
        song_id=song_id,
        song_type=song_type,
        singers=singers,
        album=album,
        duration=duration,
        media_mid=media_mid,
        raw=item,
    )


def normalize_playlist(item: dict[str, Any]) -> Playlist:
    playlist_id = str(
        item.get("tid")
        or item.get("dissid")
        or item.get("id")
        or ""
    )
    dirid = str(item.get("dirid") or item.get("tid") or item.get("dissid") or item.get("id") or "")
    name = str(
        item.get("diss_name")
        or item.get("dissname")
        or item.get("dirname")
        or item.get("name")
        or "未命名歌单"
    )
    count_value = item.get("song_cnt") or item.get("songnum") or item.get("total")
    try:
        song_count = int(count_value) if count_value is not None else None
    except (TypeError, ValueError):
        song_count = None
    cover = str(item.get("diss_cover") or item.get("logo") or item.get("cover") or "")
    return Playlist(id=playlist_id, name=name, dirid=dirid, song_count=song_count, cover=cover, raw=item)


def normalize_netease_song(item: dict[str, Any]) -> Song:
    song_id = str(item.get("id") or "")
    title = str(item.get("name") or item.get("title") or "未知歌曲")
    artists_value = item.get("ar") or item.get("artists") or []
    if isinstance(artists_value, list):
        singers = "/".join(str(artist.get("name", "")) for artist in artists_value if isinstance(artist, dict))
    else:
        singers = ""
    album_value = item.get("al") or item.get("album") or {}
    if isinstance(album_value, dict):
        album = str(album_value.get("name") or "")
    else:
        album = str(album_value or "")
    duration_value = item.get("dt") or item.get("duration")
    try:
        duration = int(duration_value) // 1000 if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    return Song(
        title=title,
        mid=song_id,
        song_id=song_id,
        singers=singers,
        album=album,
        duration=duration,
        media_mid=song_id,
        raw=item,
    )


def normalize_netease_playlist(item: dict[str, Any]) -> Playlist:
    playlist_id = str(item.get("id") or "")
    name = str(item.get("name") or "未命名歌单")
    count_value = item.get("trackCount") or item.get("song_count")
    try:
        song_count = int(count_value) if count_value is not None else None
    except (TypeError, ValueError):
        song_count = None
    cover = str(item.get("coverImgUrl") or item.get("cover") or "")
    return Playlist(id=playlist_id, name=name, dirid=playlist_id, song_count=song_count, cover=cover, raw=item)


def auth_file_path(platform: str | None = None) -> Path:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    if platform == "netease":
        return Path(os.environ.get("NETEASE_AUTH_FILE", DEFAULT_NETEASE_AUTH_FILE))
    return Path(os.environ.get("QQMUSIC_AUTH_FILE", DEFAULT_AUTH_FILE))


def settings_file_path() -> Path:
    return Path(os.environ.get("QQMUSIC_SETTINGS_FILE", DEFAULT_SETTINGS_FILE))


def load_settings() -> dict[str, Any]:
    path = settings_file_path()
    settings = dict(DEFAULT_SETTINGS)
    if not path.exists():
        return settings
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(data, dict):
        settings.update({key: value for key, value in data.items() if key in DEFAULT_SETTINGS})
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_file_path()
    data = {key: settings.get(key, value) for key, value in DEFAULT_SETTINGS.items()}
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise QQMusicError(f"保存设置失败: {exc}") from exc


def downloads_index_path(download_dir: str) -> Path:
    return Path(download_dir).expanduser() / ".qqmusic_downloads.json"


def load_download_index(download_dir: str) -> dict[str, Any]:
    path = downloads_index_path(download_dir)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_download_index(download_dir: str, index: dict[str, Any]) -> None:
    path = downloads_index_path(download_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(index, file, ensure_ascii=False, indent=2)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "未知歌曲"


def guess_audio_extension(url: str, quality: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    suffix = Path(path).suffix
    if suffix in {".mp3", ".flac", ".m4a", ".ogg", ".wav"}:
        return suffix
    return ".flac" if quality == "flac" else ".mp3"


def downloaded_songs(download_dir: str) -> list[Song]:
    root = Path(download_dir).expanduser()
    if not root.exists():
        return []
    index = load_download_index(str(root))
    songs: list[Song] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".mp3", ".flac", ".m4a", ".ogg", ".wav"}:
            continue
        meta = index.get(path.name, {}) if isinstance(index.get(path.name), dict) else {}
        songs.append(
            Song(
                title=str(meta.get("title") or path.stem),
                mid=str(meta.get("mid") or ""),
                song_id=str(meta.get("song_id") or ""),
                song_type=int(meta["song_type"]) if str(meta.get("song_type", "")).isdigit() else 0,
                singers=str(meta.get("singers") or ""),
                album=str(meta.get("album") or ""),
                duration=int(meta["duration"]) if str(meta.get("duration", "")).isdigit() else None,
                media_mid=str(meta.get("media_mid") or ""),
                local_path=str(path),
            )
        )
    return songs


def has_downloaded_songs(download_dir: str) -> bool:
    return bool(downloaded_songs(download_dir))


def is_builtin_playlist(playlist: Playlist) -> bool:
    return playlist.id == "__downloads__" or playlist.dirid == "201" or playlist.name == "我喜欢"


def is_addable_playlist(playlist: Playlist) -> bool:
    return playlist.id != "__downloads__"


def is_netease_editable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    if playlist.id == "__downloads__":
        return False
    raw = playlist.raw if isinstance(playlist.raw, dict) else {}
    if raw.get("subscribed") is True:
        return False
    try:
        special_type = int(raw.get("specialType") or 0)
    except (TypeError, ValueError):
        special_type = 0
    if special_type == 5:
        return False
    if user_id:
        creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
        creator_id = str(raw.get("userId") or creator.get("userId") or "")
        if creator_id and creator_id != user_id:
            return False
    return bool(playlist.id)


def is_netease_addable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    if playlist.id == "__downloads__":
        return False
    raw = playlist.raw if isinstance(playlist.raw, dict) else {}
    if raw.get("subscribed") is True:
        return False
    if user_id:
        creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
        creator_id = str(raw.get("userId") or creator.get("userId") or "")
        if creator_id and creator_id != user_id:
            return False
    return bool(playlist.id)


def is_netease_song_removable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    return is_netease_addable_playlist(playlist, user_id)


def load_auth(platform: str | None = None) -> dict[str, str]:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    path = auth_file_path(platform)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise QQMusicError(f"读取登录信息失败: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    auth = {str(key): str(value) for key, value in data.items() if value is not None}
    if platform == "qqmusic":
        cookie_account = qq_number_from_cookie(auth.get("cookie", ""))
        if cookie_account:
            auth["qq_number"] = cookie_account
    auth["platform"] = platform
    return auth


def save_auth(qq_number: str, cookie: str, platform: str | None = None, nickname: str = "") -> None:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    path = auth_file_path(platform)
    data = {
        "qq_number": qq_number.strip(),
        "cookie": cookie.strip(),
        "platform": platform,
        "updated_at": str(int(time.time())),
    }
    clean_nickname = nickname.strip()
    if clean_nickname and clean_nickname not in {"微信登录"}:
        data["nickname"] = clean_nickname
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise QQMusicError(f"保存登录信息失败: {exc}") from exc


def clear_auth(platform: str | None = None) -> None:
    path = auth_file_path(platform)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        raise QQMusicError(f"删除登录信息失败: {exc}") from exc


def qq_number_from_cookie(cookie: str) -> str:
    wxuin = cookie_value(cookie, "wxuin")
    if wxuin:
        digits = re.sub(r"\D", "", wxuin)
        if digits:
            return digits
    for key in ("uin", "wxuin"):
        match = re.search(rf"(?:^|;\s*){key}=([^;]+)", cookie)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return digits
    return ""


class Player:
    def __init__(self, command: str | None = None):
        explicit_command = command or os.environ.get("QQMUSIC_PLAYER")
        self.command = explicit_command
        self.backend = "external" if explicit_command else "browser"
        self.process: subprocess.Popen[str] | None = None
        self._vlc_instance: Any | None = None
        self._vlc_player: Any | None = None
        self._vlc_media: Any | None = None

        if explicit_command:
            return

        if self._init_python_vlc():
            self.backend = "python-vlc"
            return

        self.command = find_player()
        if self.command:
            self.backend = "external"

    def available(self) -> bool:
        return self.backend == "python-vlc" or bool(self.command)

    @property
    def display_name(self) -> str:
        if self.backend == "python-vlc":
            return "python-vlc"
        if self.command:
            return os.path.basename(self.command)
        return "浏览器"

    def _init_python_vlc(self) -> bool:
        try:
            vlc_module = __import__("vlc")
            self._vlc_instance = vlc_module.Instance("--no-video", "--quiet")
            self._vlc_player = self._vlc_instance.media_player_new()
        except Exception:  # noqa: BLE001 - optional backend; external player remains available.
            self._vlc_instance = None
            self._vlc_player = None
            return False
        return True

    def play(self, url: str, start_position_ms: int = 0) -> None:
        if self.backend == "python-vlc":
            self.stop()
            if not self._vlc_instance or not self._vlc_player:
                raise QQMusicError("python-vlc 初始化失败")
            self._vlc_media = self._vlc_instance.media_new(url)
            self._vlc_player.set_media(self._vlc_media)
            if self._vlc_player.play() == -1:
                raise QQMusicError("python-vlc 播放失败")
            return

        if not self.command:
            webbrowser.open(url)
            return

        self.stop()
        args = build_player_args(self.command, url, start_position_ms)
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pause(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            if hasattr(self._vlc_player, "set_pause"):
                self._vlc_player.set_pause(True)
            else:
                self._vlc_player.pause()
            return True
        self.stop()
        return False

    def resume(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            if hasattr(self._vlc_player, "set_pause"):
                self._vlc_player.set_pause(False)
            else:
                self._vlc_player.play()
            return True
        return False

    def stop(self) -> None:
        if self._vlc_player is not None:
            self._vlc_player.stop()
            self._vlc_media = None

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def has_ended(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            state = str(self._vlc_player.get_state())
            return state.endswith(".Ended") or state.endswith(".Error")
        if self.process is not None:
            return self.process.poll() is not None
        return False

    def position_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            value = self._vlc_player.get_time()
            return int(value) if value is not None and value >= 0 else None
        return None

    def duration_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            value = self._vlc_player.get_length()
            return int(value) if value is not None and value > 0 else None
        return None

    def buffered_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            duration = self.duration_ms()
            position = self.position_ms()
            is_seekable = False
            try:
                is_seekable = bool(self._vlc_player.is_seekable())
            except Exception:
                is_seekable = False
            if is_seekable and duration:
                return duration
            return position if duration and position and position >= duration else None
        return None

    def seek_ms(self, position_ms: int) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            target = max(0, int(position_ms))
            self._vlc_player.set_time(target)
            duration = self.duration_ms()
            if duration:
                try:
                    self._vlc_player.set_position(max(0.0, min(1.0, target / duration)))
                except Exception:
                    pass
            return True
        return False


def find_player() -> str | None:
    for name in ("mpv", "vlc", "ffplay", "xdg-open"):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_player_args(command: str, url: str, start_position_ms: int = 0) -> list[str]:
    name = os.path.basename(command)
    start_seconds = max(0.0, start_position_ms / 1000)
    if name == "mpv":
        args = [command, "--force-window=yes"]
        if start_seconds:
            args.append(f"--start={start_seconds:.3f}")
        args.append(url)
        return args
    if name == "vlc":
        args = [command, "--started-from-file"]
        if start_seconds:
            args.append(f"--start-time={start_seconds:.3f}")
        args.append(url)
        return args
    if name == "ffplay":
        args = [command, "-autoexit", "-nodisp"]
        if start_seconds:
            args.extend(["-ss", f"{start_seconds:.3f}"])
        args.append(url)
        return args
    return [command, url]


def build_music_api(platform: str, api_base: str = DEFAULT_API_BASE, timeout: int = DEFAULT_TIMEOUT) -> Any:
    normalized = normalize_platform(platform)
    if normalized == "netease":
        return NeteaseMusicAPI(timeout=timeout)
    return QQMusicAPI(base_url=api_base, timeout=timeout)


def paeth_predictor(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def png_luminance_grid(image: bytes) -> list[list[int]]:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise QQMusicError("二维码不是 PNG 格式，无法直接在终端显示")

    offset = 8
    width = height = bit_depth = color_type = interlace = 0
    palette: list[tuple[int, int, int]] = []
    idat_parts: list[bytes] = []
    while offset + 8 <= len(image):
        length = int.from_bytes(image[offset : offset + 4], "big")
        chunk_type = image[offset + 4 : offset + 8]
        data = image[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width = int.from_bytes(data[0:4], "big")
            height = int.from_bytes(data[4:8], "big")
            bit_depth = data[8]
            color_type = data[9]
            interlace = data[12]
        elif chunk_type == b"PLTE":
            palette = [
                (data[index], data[index + 1], data[index + 2])
                for index in range(0, len(data) - 2, 3)
            ]
        elif chunk_type == b"IDAT":
            idat_parts.append(data)
        elif chunk_type == b"IEND":
            break

    if not width or not height or not idat_parts:
        raise QQMusicError("二维码 PNG 数据不完整")
    if interlace:
        raise QQMusicError("暂不支持隔行 PNG 二维码")

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type.get(color_type)
    if channels is None:
        raise QQMusicError("暂不支持这个 PNG 颜色格式")

    bits_per_pixel = channels * bit_depth
    scanline_size = (width * bits_per_pixel + 7) // 8
    filter_bytes_per_pixel = max(1, (bits_per_pixel + 7) // 8)
    raw = zlib.decompress(b"".join(idat_parts))
    rows: list[bytes] = []
    previous = bytes(scanline_size)
    cursor = 0
    for _row_index in range(height):
        filter_type = raw[cursor]
        cursor += 1
        current = bytearray(raw[cursor : cursor + scanline_size])
        cursor += scanline_size
        for index, value in enumerate(current):
            left = current[index - filter_bytes_per_pixel] if index >= filter_bytes_per_pixel else 0
            up = previous[index]
            up_left = previous[index - filter_bytes_per_pixel] if index >= filter_bytes_per_pixel else 0
            if filter_type == 1:
                current[index] = (value + left) & 0xFF
            elif filter_type == 2:
                current[index] = (value + up) & 0xFF
            elif filter_type == 3:
                current[index] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                current[index] = (value + paeth_predictor(left, up, up_left)) & 0xFF
            elif filter_type != 0:
                raise QQMusicError("二维码 PNG 使用了未知滤镜")
        previous = bytes(current)
        rows.append(previous)

    def unpack_sample(row: bytes, x: int, sample_index: int = 0) -> int:
        if bit_depth == 8:
            return row[x * channels + sample_index]
        if bit_depth == 16:
            return row[(x * channels + sample_index) * 2]
        bit_offset = (x * channels + sample_index) * bit_depth
        byte = row[bit_offset // 8]
        shift = 8 - bit_depth - (bit_offset % 8)
        value = (byte >> shift) & ((1 << bit_depth) - 1)
        return int(value * 255 / ((1 << bit_depth) - 1))

    grid: list[list[int]] = []
    for row in rows:
        luminance_row: list[int] = []
        for x in range(width):
            if color_type == 0:
                luminance = unpack_sample(row, x)
            elif color_type == 3:
                palette_index = unpack_sample(row, x)
                red, green, blue = palette[palette_index] if palette_index < len(palette) else (255, 255, 255)
                luminance = int(red * 0.299 + green * 0.587 + blue * 0.114)
            else:
                red = unpack_sample(row, x, 0)
                green = unpack_sample(row, x, 1)
                blue = unpack_sample(row, x, 2)
                luminance = int(red * 0.299 + green * 0.587 + blue * 0.114)
            luminance_row.append(luminance)
        grid.append(luminance_row)
    return grid


def estimate_qr_pixel_scale(width: int, height: int) -> int:
    best = 1
    for scale in range(2, 13):
        if width % scale == 0 and height % scale == 0 and 21 <= min(width, height) // scale <= 80:
            best = scale
    return best


def downsample_qr_grid(luminance: list[list[int]]) -> list[list[bool]]:
    height = len(luminance)
    width = len(luminance[0]) if height else 0
    scale = estimate_qr_pixel_scale(width, height)
    result: list[list[bool]] = []
    for y in range(0, height, scale):
        row: list[bool] = []
        for x in range(0, width, scale):
            total = 0
            count = 0
            for block_y in range(y, min(y + scale, height)):
                for block_x in range(x, min(x + scale, width)):
                    total += luminance[block_y][block_x]
                    count += 1
            row.append((total / max(1, count)) < 128)
        result.append(row)
    return result


def terminal_qr_lines(image: bytes) -> list[str]:
    modules = downsample_qr_grid(png_luminance_grid(image))
    lines: list[str] = []
    reset = "\033[0m"
    for y in range(0, len(modules), 2):
        top = modules[y]
        bottom = modules[y + 1] if y + 1 < len(modules) else [False] * len(top)
        parts: list[str] = []
        for top_dark, bottom_dark in zip(top, bottom):
            foreground = "30" if top_dark else "97"
            background = "40" if bottom_dark else "107"
            parts.append(f"\033[{foreground};{background}m▀")
        lines.append("".join(parts) + reset)
    return lines


def print_terminal_qr(image: bytes) -> bool:
    try:
        for line in terminal_qr_lines(image):
            print(line)
    except QQMusicError as exc:
        print(f"终端二维码显示失败: {exc}")
        return False
    return True


def terminal_song_line(song: Song) -> str:
    parts = [song.title]
    if song.singers:
        parts.append(f"- {song.singers}")
    if song.album:
        parts.append(f"[{song.album}]")
    if song.duration:
        parts.append(format_seconds(song.duration))
    return "  ".join(parts)


def parse_one_based_index(value: str, size: int, label: str) -> int:
    try:
        index = int(value)
    except ValueError as exc:
        raise QQMusicError(f"{label}必须是数字") from exc
    if not 1 <= index <= size:
        raise QQMusicError(f"{label}超出范围: 1-{size}")
    return index - 1


def setup_command_history() -> Any | None:
    try:
        import readline
    except ImportError:
        return None

    try:
        readline.clear_history()
        readline.set_history_length(1000)
    except Exception:
        pass
    return readline


def remember_command(readline_module: Any | None, line: str) -> None:
    if readline_module is None or not line:
        return
    try:
        history_length = readline_module.get_current_history_length()
        last_item = readline_module.get_history_item(history_length) if history_length else ""
        if last_item != line:
            readline_module.add_history(line)
    except Exception:
        pass


def download_song_file(
    api: Any,
    song: Song,
    quality: str,
    cookie: str,
    download_dir: str,
    timeout: int,
) -> Path:
    if song.local_path:
        raise QQMusicError("这首歌已经是本地文件")
    url = api.song_url(song.mid, quality, cookie, song.media_mid)
    target_dir = Path(download_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    extension = guess_audio_extension(url, quality)
    base_name = safe_filename(f"{song.title} - {song.singers}" if song.singers else song.title)
    target = target_dir / f"{base_name}{extension}"
    counter = 2
    while target.exists():
        target = target_dir / f"{base_name} ({counter}){extension}"
        counter += 1

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 music-linux-client/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as file:
            shutil.copyfileobj(response, file)
    except urllib.error.URLError as exc:
        raise QQMusicError(f"下载失败: {exc.reason}") from exc
    except OSError as exc:
        raise QQMusicError(f"写入文件失败: {exc}") from exc

    index = load_download_index(str(target_dir))
    index[target.name] = {
        "title": song.title,
        "mid": song.mid,
        "song_id": song.song_id,
        "song_type": song.song_type,
        "singers": song.singers,
        "album": song.album,
        "duration": song.duration,
        "media_mid": song.media_mid,
        "quality": quality,
        "downloaded_at": int(time.time()),
    }
    save_download_index(str(target_dir), index)
    return target


class TerminalMusicApp:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.settings = load_settings()
        self.platform = normalize_platform(args.platform or str(self.settings.get("platform", "qqmusic")))
        self.api = build_music_api(self.platform, args.api_base, args.timeout)
        self.player = Player(args.player)
        self.auth = load_auth(self.platform)
        self.songs: list[Song] = []
        self.playlists: list[Playlist] = []
        self.current_playlist: Playlist | None = None
        self.current_song_index = -1
        self.playback_started_at = 0.0
        self.playback_token = 0
        self.lyric_mode_active = False
        self.quality = str(self.settings.get("quality", "320"))
        self.play_mode = normalize_play_mode(str(self.settings.get("play_mode", "顺序播放")))
        self.readline = setup_command_history()
        atexit.register(self.player.stop)

    def run(self) -> int:
        print("QQMusic TUI")
        print("输入 help 查看命令，输入 quit 退出。")
        self.print_status()
        while True:
            try:
                line = input("qqmusic> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self.player.stop()
                return 0
            if not line:
                continue
            remember_command(self.readline, line)
            try:
                if self.handle_command(line):
                    return 0
            except QQMusicError as exc:
                print(f"错误: {exc}")
            except Exception as exc:  # noqa: BLE001 - keep the terminal session alive.
                print(f"错误: {exc}")

    def handle_command(self, line: str) -> bool:
        parts = shlex.split(line)
        command = parts[0].lower()
        args = parts[1:]

        if command in {"quit", "exit", "q"}:
            self.player.stop()
            return True
        if command in {"help", "h", "?"}:
            self.print_help()
            return False
        if command in {"status", "whoami"}:
            self.print_status()
            return False
        if command == "platform":
            self.command_platform(args)
            return False
        if command == "quality":
            self.command_quality(args)
            return False
        if command in {"mode", "playmode", "play-mode", "order"}:
            self.command_mode(args)
            return False
        if command == "login":
            self.command_login(args)
            return False
        if command == "logout":
            clear_auth(self.platform)
            self.auth = {}
            print("已退出登录")
            return False
        if command in {"search", "s"}:
            self.command_search(args)
            return False
        if command in {"songs", "queue", "list"}:
            self.print_songs()
            return False
        if command in {"play", "p"}:
            self.command_play(args)
            return False
        if command in {"next", "n"}:
            self.command_next(reverse=False)
            return False
        if command in {"prev", "previous"}:
            self.command_next(reverse=True)
            return False
        if command == "pause":
            if self.player.pause():
                self.playback_token += 1
                print("已暂停")
            else:
                self.playback_token += 1
                print("当前播放器不支持暂停，已停止播放")
            return False
        if command in {"resume", "continue"}:
            if self.player.resume():
                self.playback_started_at = time.monotonic()
                self.playback_token += 1
                self.start_playback_monitor(self.playback_token)
                print("继续播放")
            return False
        if command == "stop":
            self.playback_token += 1
            self.player.stop()
            print("已停止")
            return False
        if command in {"lyric", "lyrics", "l"}:
            self.command_lyric(args)
            return False
        if command in {"playlists", "pls"}:
            self.command_playlists()
            return False
        if command in {"playlist", "pl"}:
            if args:
                self.command_open_playlist(args)
            else:
                self.command_playlists()
            return False
        if command in {"open", "load"}:
            self.command_open_playlist(args)
            return False
        if command == "create":
            self.command_create_playlist(args)
            return False
        if command == "rename":
            self.command_rename_playlist(args)
            return False
        if command == "delete":
            self.command_delete_playlist(args)
            return False
        if command == "add":
            self.command_add_song(args)
            return False
        if command == "remove":
            self.command_remove_song(args)
            return False
        if command in {"download", "dl"}:
            self.command_download(args)
            return False
        if command == "settings":
            print(json.dumps(self.settings, ensure_ascii=False, indent=2))
            return False

        print(f"未知命令: {command}")
        print("输入 help 查看可用命令。")
        return False

    def print_help(self) -> None:
        print(
            """
推荐流程:
  login qq                       登录
  search 稻香                    搜索歌曲，然后 play 1 播放
  playlist                       查看我的歌单
  playlist 1                     打开第 1 个歌单，然后 play 1 播放
  play playlist 1                打开第 1 个歌单并立刻播放第一首

常用命令:
  status                         显示当前平台、账号和播放器
  platform [qqmusic|netease]     查看或切换平台
  quality [128|320|flac]         查看或设置播放/下载音质
  mode [order|random|repeat]      切换或设置播放顺序
  login [qq|wx|cookie|phone]     登录；QQ 支持 qq/wx/cookie，网易云支持 cookie/phone
  logout                         删除当前平台的本地登录信息
  search <关键词> [-n 数量]       搜索歌曲
  songs                          显示当前歌曲队列前 100 首
  play [歌曲序号]                播放当前队列；不填序号时播放当前/第一首
  play playlist <歌单序号>        打开歌单并播放第一首
  next | prev                    手动切到下一首/上一首；播完也会自动切歌
  pause | resume | stop          控制播放
  lyric [序号]                   实时显示歌词；播完自动切歌，Ctrl+C 返回
  playlist                       同步并显示我的歌单
  playlist <歌单序号|id>         打开歌单到歌曲队列
  open <歌单序号|id>             同 playlist <歌单序号|id>
  add <歌曲序号> <歌单序号>       把歌曲加入歌单
  remove <歌曲序号>              从当前歌单移除歌曲
  create <歌单名>                新建歌单
  rename <歌单序号> <新名字>     重命名歌单
  delete <歌单序号>              删除歌单
  download <歌曲序号>            下载歌曲到设置中的下载目录
  quit                           退出
""".strip()
        )

    def print_status(self) -> None:
        account = self.auth.get("nickname") or self.auth.get("qq_number") or "未登录"
        print(
            f"平台: {platform_display_name(self.platform)} | "
            f"账号: {account} | "
            f"音质: {self.quality} | "
            f"播放顺序: {self.play_mode} | "
            f"播放器: {self.player.display_name}"
        )

    def refresh_api(self) -> None:
        self.api = build_music_api(self.platform, self.args.api_base, self.args.timeout)
        self.auth = load_auth(self.platform)
        self.songs = []
        self.playlists = []
        self.current_playlist = None
        self.current_song_index = -1

    def command_platform(self, args: list[str]) -> None:
        if not args:
            print(self.platform)
            return
        raw_platform = args[0]
        if raw_platform not in PLATFORMS:
            raise QQMusicError("平台只能是 qqmusic 或 netease")
        new_platform = normalize_platform(raw_platform)
        if new_platform == self.platform:
            print(f"当前已经是 {platform_display_name(self.platform)}")
            return
        self.player.stop()
        self.platform = new_platform
        self.settings["platform"] = new_platform
        save_settings(self.settings)
        self.refresh_api()
        print(f"已切换到 {platform_display_name(self.platform)}")

    def command_quality(self, args: list[str]) -> None:
        if not args:
            print(self.quality)
            return
        quality = args[0]
        if quality not in {"128", "320", "flac"}:
            raise QQMusicError("音质只能是 128、320 或 flac")
        self.quality = quality
        self.settings["quality"] = quality
        save_settings(self.settings)
        print(f"已设置音质: {quality}")

    def command_mode(self, args: list[str]) -> None:
        if not args:
            current = PLAY_MODES.index(self.play_mode) if self.play_mode in PLAY_MODES else 0
            new_mode = PLAY_MODES[(current + 1) % len(PLAY_MODES)]
        else:
            raw_mode = " ".join(args).strip()
            normalized = normalize_play_mode(raw_mode)
            if raw_mode.lower() not in PLAY_MODE_ALIASES and raw_mode not in PLAY_MODES:
                raise QQMusicError("播放顺序只能是 order、random、repeat，或 顺序播放、随机播放、单曲循环")
            new_mode = normalized
        self.play_mode = new_mode
        self.settings["play_mode"] = new_mode
        save_settings(self.settings)
        print(f"播放顺序: {self.play_mode}")

    def command_login(self, args: list[str]) -> None:
        method = args[0].lower() if args else ""
        if self.platform == "netease":
            if method not in {"", "cookie", "phone"}:
                raise QQMusicError("网易云登录方式只能是 cookie 或 phone")
            if not method:
                method = input("登录方式 cookie/phone [cookie]: ").strip().lower() or "cookie"
            if method == "cookie":
                cookie = input("粘贴网易云 Cookie: ").strip()
                result = self.api.login_with_cookie(cookie)
            else:
                phone = args[1] if len(args) > 1 else input("手机号: ").strip()
                country_code = args[2] if len(args) > 2 else input("国家码 [86]: ").strip() or "86"
                self.api.send_phone_captcha(phone, country_code)
                captcha = input("验证码: ").strip()
                result = self.api.login_with_phone_captcha(phone, captcha, country_code)
            save_auth(result.qq_number, result.cookie, self.platform, result.nickname)
            self.auth = load_auth(self.platform)
            print(f"登录成功: {result.nickname or result.qq_number}")
            return

        if method not in {"", "qq", "wx", "cookie"}:
            raise QQMusicError("QQ 音乐登录方式只能是 qq、wx 或 cookie")
        if not method:
            method = input("登录方式 qq/wx/cookie [qq]: ").strip().lower() or "qq"
        if method == "cookie":
            cookie = input("粘贴 QQ 音乐 Cookie: ").strip()
            qq_number = qq_number_from_cookie(cookie) or input("QQ 号: ").strip()
            if not qq_number:
                raise QQMusicError("没有 QQ 号。Cookie 里需要包含 uin/wxuin，或手动输入 QQ 号")
            save_auth(qq_number, cookie, self.platform)
            self.auth = load_auth(self.platform)
            print(f"已保存登录信息: {qq_number}")
            return
        if method == "wx":
            session = self.api.start_wx_qr_login()
            qr_path = Path.cwd() / "qqmusic_tui_wx_login_qr.png"
            qr_path.write_bytes(session.image)
            print("请用微信扫描下面的二维码:")
            print_terminal_qr(session.image)
            print(f"如果终端显示不清楚，也可以打开图片: {qr_path}")
            poll = self.api.poll_wx_qr_login
        else:
            session = self.api.start_qr_login()
            qr_path = Path.cwd() / "qqmusic_tui_qq_login_qr.png"
            qr_path.write_bytes(session.image)
            print("请用手机 QQ 扫描下面的二维码:")
            print_terminal_qr(session.image)
            print(f"如果终端显示不清楚，也可以打开图片: {qr_path}")
            poll = self.api.poll_qr_login
        print("扫码后在手机上确认登录，程序会自动继续。")
        deadline = time.time() + int(getattr(self.args, "timeout_seconds", 180))
        last_state = ""
        while time.time() < deadline:
            state, result = poll(session)
            if state != last_state:
                if state == "waiting":
                    print("等待扫码...")
                elif state == "confirming":
                    print("已扫码，等待手机确认...")
                elif state == "expired":
                    raise QQMusicError("二维码已过期，请重新运行 login")
                elif state == "cancelled":
                    raise QQMusicError("已取消登录")
                last_state = state
            if state == "done" and result:
                save_auth(result.qq_number, result.cookie, self.platform, result.nickname)
                self.auth = load_auth(self.platform)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return
            time.sleep(2)
        raise QQMusicError("登录超时，请重新运行 login")

    def command_search(self, args: list[str]) -> None:
        if not args:
            raise QQMusicError("用法: search <关键词> [-n 数量]")
        count = 10
        if "-n" in args:
            pos = args.index("-n")
            if pos + 1 >= len(args):
                raise QQMusicError("-n 后面需要数量")
            count = int(args[pos + 1])
            args = args[:pos] + args[pos + 2 :]
        keyword = " ".join(args).strip()
        if not keyword:
            raise QQMusicError("关键词不能为空")
        self.songs = self.api.search(keyword, count=count, cookie=self.auth.get("cookie", ""))
        self.current_playlist = None
        self.current_song_index = -1
        self.print_songs()
        if self.songs:
            print("输入 play 1 播放第一首，或 next 从队列开始播放。")

    def print_songs(self) -> None:
        if not self.songs:
            print("当前队列为空")
            return
        display_limit = 100
        shown_songs = self.songs[:display_limit]
        for index, song in enumerate(shown_songs, 1):
            marker = "*" if index - 1 == self.current_song_index else " "
            print(f"{marker}{index:02d}. {terminal_song_line(song)}")
            if song.mid:
                print(f"     mid: {song.mid}")
        remaining = len(self.songs) - len(shown_songs)
        if remaining > 0:
            print(f"... 还有 {remaining} 首未显示。可直接用序号播放，例如 play 101。")

    def selected_song_index(self, args: list[str], default_current: bool = False) -> int:
        if not self.songs:
            raise QQMusicError("当前没有歌曲队列，先 search 或 open 歌单")
        if args:
            return parse_one_based_index(args[0], len(self.songs), "歌曲序号")
        elif default_current and 0 <= self.current_song_index < len(self.songs):
            return self.current_song_index
        return 0

    def selected_song(self, args: list[str], default_current: bool = False) -> Song:
        return self.songs[self.selected_song_index(args, default_current)]

    def command_play(self, args: list[str]) -> None:
        if args and args[0].lower() in {"playlist", "playlists", "pl"}:
            if len(args) < 2:
                self.command_playlists()
                print("用法: play playlist <歌单序号|id>")
                return
            self.command_open_playlist(args[1:], autoplay=True)
            return
        self.play_song_at(self.selected_song_index(args, default_current=True))

    def play_song_at(self, index: int) -> None:
        if not 0 <= index < len(self.songs):
            raise QQMusicError("歌曲序号超出范围")
        song = self.songs[index]
        url = self.api.song_url(song.mid, self.quality, self.auth.get("cookie", ""), song.media_mid)
        self.player.play(url)
        self.current_song_index = index
        self.playback_started_at = time.monotonic()
        self.playback_token += 1
        self.start_playback_monitor(self.playback_token)
        print(f"正在播放: {song.display}")

    def start_playback_monitor(self, token: int) -> None:
        threading.Thread(target=self.monitor_playback, args=(token,), daemon=True).start()

    def monitor_playback(self, token: int) -> None:
        time.sleep(3)
        while token == self.playback_token:
            if self.lyric_mode_active:
                time.sleep(0.5)
                continue
            if self.player.has_ended():
                next_index = self.next_index_for_auto_advance()
                if next_index < 0:
                    print("\n播放完成")
                    return
                try:
                    print()
                    self.play_song_at(next_index)
                except QQMusicError as exc:
                    print(f"\n自动播放下一首失败: {exc}")
                return
            time.sleep(1)

    def next_index_for_manual_skip(self, reverse: bool = False) -> int:
        if not self.songs:
            raise QQMusicError("当前没有歌曲队列，先 search 或 open 歌单")
        if self.current_song_index < 0:
            return len(self.songs) - 1 if reverse else 0
        index = self.current_song_index
        if self.play_mode == "随机播放":
            if len(self.songs) == 1:
                return index
            choices = [candidate for candidate in range(len(self.songs)) if candidate != index]
            return random.choice(choices)
        if reverse:
            return index - 1 if index > 0 else len(self.songs) - 1
        next_index = index + 1
        return next_index if next_index < len(self.songs) else 0

    def next_index_for_auto_advance(self) -> int:
        if not self.songs:
            return -1
        index = self.current_song_index if self.current_song_index >= 0 else 0
        if self.play_mode == "单曲循环":
            return index
        if self.play_mode == "随机播放":
            if len(self.songs) == 1:
                return index
            choices = [candidate for candidate in range(len(self.songs)) if candidate != index]
            return random.choice(choices)
        next_index = index + 1
        return next_index if next_index < len(self.songs) else -1

    def command_next(self, reverse: bool = False) -> None:
        self.play_song_at(self.next_index_for_manual_skip(reverse))

    def command_lyric(self, args: list[str]) -> None:
        song_index = self.selected_song_index(args, default_current=True)
        follows_current_song = song_index == self.current_song_index

        def load_lyric(target_index: int) -> tuple[Song, list[tuple[int, str]]]:
            target_song = self.songs[target_index]
            try:
                lyric = self.api.lyric(target_song.mid, self.auth.get("cookie", ""))
                return target_song, parse_lrc_lines(lyric)
            except QQMusicError as exc:
                print(f"\n歌词加载失败: {exc}")
                return target_song, []

        song, lines = load_lyric(song_index)
        if not lines and not follows_current_song:
            print("没有可同步歌词")
            return

        preview_started_at = time.monotonic()
        print(f"实时歌词: {song.display}")
        if follows_current_song:
            print("按 Ctrl+C 返回命令行。")
            if not lines:
                print("当前歌曲没有可同步歌词，仍会在歌曲结束后自动切歌。")
        else:
            print("这首歌当前未播放，将从 0:00 开始预览歌词。按 Ctrl+C 返回命令行。")

        last_line_index = -1
        completion_reported = False
        self.lyric_mode_active = True
        try:
            while True:
                if completion_reported:
                    time.sleep(0.2)
                    continue

                if follows_current_song:
                    position = self.player.position_ms()
                    if position is None and self.playback_started_at:
                        position = int((time.monotonic() - self.playback_started_at) * 1000)
                else:
                    position = int((time.monotonic() - preview_started_at) * 1000)
                if position is None:
                    position = 0

                if lines:
                    line_index = -1
                    for index, (line_time, _text) in enumerate(lines):
                        if line_time <= position:
                            line_index = index
                        else:
                            break

                    if line_index >= 0 and line_index != last_line_index:
                        line_time, text = lines[line_index]
                        print(f"\r\033[K{format_milliseconds(line_time)}  {text}", end="", flush=True)
                        last_line_index = line_index

                if follows_current_song and self.player.has_ended():
                    next_index = self.next_index_for_auto_advance()
                    if next_index < 0:
                        print("\r\033[K播放完成，按 Ctrl+C 返回命令行。", flush=True)
                        completion_reported = True
                        continue
                    print()
                    self.play_song_at(next_index)
                    song, lines = load_lyric(next_index)
                    print(f"实时歌词: {song.display}")
                    if not lines:
                        print("当前歌曲没有可同步歌词，仍会在歌曲结束后自动切歌。")
                    last_line_index = -1
                    continue
                if not follows_current_song and position > lines[-1][0] + 3000:
                    print()
                    return
                time.sleep(0.2)
        except KeyboardInterrupt:
            print()
        finally:
            self.lyric_mode_active = False

    def command_playlists(self) -> None:
        account = self.auth.get("qq_number", "")
        if not account:
            raise QQMusicError("还没有登录，先运行 login")
        self.playlists = self.api.user_playlists(account, self.auth.get("cookie", ""))
        download_dir = str(self.settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))
        if has_downloaded_songs(download_dir):
            self.playlists.append(Playlist(id="__downloads__", name="已下载的歌曲", dirid="__downloads__"))
        if not self.playlists:
            print("没有歌单")
            return
        for index, playlist in enumerate(self.playlists, 1):
            print(f"{index:02d}. {playlist.display}")
            print(f"    id: {playlist.id} dirid: {playlist.dirid}")
        print("输入 playlist 1 打开歌单，或 play playlist 1 直接播放这个歌单。")

    def selected_playlist_by_token(self, token: str) -> Playlist:
        if not self.playlists:
            self.command_playlists()
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(self.playlists):
                return self.playlists[index - 1]
        for playlist in self.playlists:
            if playlist.id == token or playlist.dirid == token:
                return playlist
        raise QQMusicError("没有找到歌单")

    def command_open_playlist(self, args: list[str], autoplay: bool = False) -> None:
        if not args:
            self.command_playlists()
            print("用法: playlist <歌单序号|id>，例如 playlist 1")
            return
        playlist = self.selected_playlist_by_token(args[0])
        if playlist.id == "__downloads__":
            self.songs = downloaded_songs(str(self.settings.get("download_dir", DEFAULT_SETTINGS["download_dir"])))
            name = playlist.name
        else:
            name, self.songs = self.api.playlist_songs(playlist.id, self.auth.get("cookie", ""))
        self.current_playlist = playlist
        self.current_song_index = -1
        print(f"已打开: {name} ({len(self.songs)} 首)")
        if autoplay:
            if not self.songs:
                print("这个歌单没有歌曲")
                return
            self.play_song_at(0)
            return
        self.print_songs()
        if self.songs:
            print("输入 play 1 播放第一首，或 play playlist 1 下次直接打开并播放。")

    def command_create_playlist(self, args: list[str]) -> None:
        name = " ".join(args).strip() or input("歌单名: ").strip()
        if not name:
            raise QQMusicError("歌单名不能为空")
        playlist_id = self.api.create_playlist(name, self.auth.get("cookie", ""))
        print(f"已创建歌单: {name} {playlist_id}".rstrip())
        self.command_playlists()

    def command_rename_playlist(self, args: list[str]) -> None:
        if len(args) < 2:
            raise QQMusicError("用法: rename <歌单序号> <新名字>")
        playlist = self.selected_playlist_by_token(args[0])
        if self.platform == "netease":
            if not is_netease_editable_playlist(playlist, self.auth.get("qq_number", "")):
                raise QQMusicError("这个网易云歌单不能重命名")
        elif is_builtin_playlist(playlist):
            raise QQMusicError("内置歌单不能重命名")
        new_name = " ".join(args[1:]).strip()
        self.api.rename_playlist(playlist.dirid, new_name, self.auth.get("cookie", ""))
        print("已重命名")
        self.command_playlists()

    def command_delete_playlist(self, args: list[str]) -> None:
        if not args:
            raise QQMusicError("用法: delete <歌单序号>")
        playlist = self.selected_playlist_by_token(args[0])
        if self.platform == "netease":
            if not is_netease_editable_playlist(playlist, self.auth.get("qq_number", "")):
                raise QQMusicError("这个网易云歌单不能删除")
        elif is_builtin_playlist(playlist):
            raise QQMusicError("内置歌单不能删除")
        confirm = input(f"确认删除“{playlist.name}”？输入 yes 继续: ").strip().lower()
        if confirm != "yes":
            print("已取消")
            return
        self.api.delete_playlist(playlist.dirid, self.auth.get("cookie", ""))
        print("已删除")
        self.command_playlists()

    def command_add_song(self, args: list[str]) -> None:
        if len(args) < 2:
            raise QQMusicError("用法: add <歌曲序号> <歌单序号>")
        song = self.songs[parse_one_based_index(args[0], len(self.songs), "歌曲序号")]
        playlist = self.selected_playlist_by_token(args[1])
        if self.platform == "netease":
            if not is_netease_addable_playlist(playlist, self.auth.get("qq_number", "")):
                raise QQMusicError("这个网易云歌单不能添加歌曲")
        elif not is_addable_playlist(playlist):
            raise QQMusicError("这个歌单不能添加歌曲")
        self.api.add_song_to_playlist(
            song.song_id or song.mid,
            song.song_type,
            playlist.dirid,
            self.auth.get("cookie", ""),
            song.mid,
            playlist.id,
        )
        print(f"已加入: {playlist.name}")

    def command_remove_song(self, args: list[str]) -> None:
        if not args:
            raise QQMusicError("用法: remove <歌曲序号>")
        if not self.current_playlist:
            raise QQMusicError("当前队列不是从歌单打开的")
        index = parse_one_based_index(args[0], len(self.songs), "歌曲序号")
        song = self.songs[index]
        if self.current_playlist.id == "__downloads__":
            if not song.local_path:
                raise QQMusicError("本地文件路径为空")
            path = Path(song.local_path)
            try:
                path.unlink()
            except OSError as exc:
                raise QQMusicError(f"删除本地文件失败: {exc}") from exc
            index_data = load_download_index(str(path.parent))
            index_data.pop(path.name, None)
            save_download_index(str(path.parent), index_data)
            del self.songs[index]
            print("已删除本地文件")
            return
        if self.platform == "netease":
            if not is_netease_song_removable_playlist(self.current_playlist, self.auth.get("qq_number", "")):
                raise QQMusicError("这个网易云歌单不能移除歌曲")
        elif is_builtin_playlist(self.current_playlist):
            raise QQMusicError("内置歌单不能移除歌曲")
        self.api.remove_song_from_playlist(
            song.song_id or song.mid,
            song.song_type,
            self.current_playlist.dirid,
            self.auth.get("cookie", ""),
        )
        del self.songs[index]
        print("已移除歌曲")

    def command_download(self, args: list[str]) -> None:
        song = self.selected_song(args, default_current=True)
        target = download_song_file(
            self.api,
            song,
            self.quality,
            self.auth.get("cookie", ""),
            str(self.settings.get("download_dir", DEFAULT_SETTINGS["download_dir"])),
            self.args.timeout,
        )
        print(f"已下载: {target}")


def run_tui(args: argparse.Namespace) -> int:
    return TerminalMusicApp(args).run()


def run_cli(args: argparse.Namespace) -> int:
    platform = normalize_platform(args.platform)
    api = build_music_api(platform, args.api_base, args.timeout)

    if args.command == "login":
        if platform == "netease":
            if args.cookie:
                result = api.login_with_cookie(args.cookie)
                save_auth(result.qq_number, result.cookie, platform, result.nickname)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return 0
            if args.send_captcha:
                if not args.phone:
                    raise QQMusicError("发送验证码需要 --phone")
                api.send_phone_captcha(args.phone, args.country_code)
                print("验证码已发送")
                return 0
            if args.phone and args.captcha:
                result = api.login_with_phone_captcha(args.phone, args.captcha, args.country_code)
                save_auth(result.qq_number, result.cookie, platform, result.nickname)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return 0
            raise QQMusicError("网易云命令行登录请使用 --cookie，或 --send-captcha/--phone/--captcha")

        if args.cookie:
            qq_number = args.qq_number or qq_number_from_cookie(args.cookie)
            if not qq_number:
                raise QQMusicError("没有 QQ 号。请传入 QQ 号，或提供包含 uin/wxuin 的 Cookie")
            save_auth(qq_number, args.cookie, platform)
            print(f"已保存登录信息: {qq_number}")
            return 0

        session = api.start_qr_login()
        qr_path = Path.cwd() / "music_login_qr.png"
        qr_path.write_bytes(session.image)
        print("请用手机 QQ 扫描下面的二维码:", flush=True)
        print_terminal_qr(session.image)
        print(f"如果终端显示不清楚，也可以打开图片: {qr_path}", flush=True)
        print("扫码后在手机上确认登录，程序会自动继续。", flush=True)
        deadline = time.time() + args.timeout_seconds
        while time.time() < deadline:
            state, result = api.poll_qr_login(session)
            if state == "waiting":
                print("等待扫码...", flush=True)
            elif state == "confirming":
                print("已扫码，等待手机确认...", flush=True)
            elif state == "expired":
                raise QQMusicError("二维码已过期，请重新运行 login")
            elif state == "done" and result:
                save_auth(result.qq_number, result.cookie, platform, result.nickname)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return 0
            time.sleep(2)
        raise QQMusicError("登录超时，请重新运行 login")
        return 0

    if args.command == "logout":
        clear_auth(platform)
        print("已退出登录")
        return 0

    if args.command == "sync-playlists":
        auth = load_auth(platform)
        qq_number = args.qq_number or auth.get("qq_number", "")
        if not qq_number:
            raise QQMusicError("还没有登录。先运行 login，或给 sync-playlists 传入账号 ID")
        playlists = api.user_playlists(qq_number, auth.get("cookie", ""))
        for index, playlist in enumerate(playlists, 1):
            print(f"{index:02d}. {playlist.display}")
            print(f"    id: {playlist.id}")
        return 0

    if args.command == "search":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        for index, song in enumerate(songs, 1):
            print(f"{index:02d}. {song.display}")
            print(f"    mid: {song.mid}")
        return 0

    if args.command == "play":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        if not songs:
            raise QQMusicError("没有搜索到歌曲")
        song = songs[min(args.index - 1, len(songs) - 1)]
        url = api.song_url(song.mid, args.quality, auth.get("cookie", ""), song.media_mid)
        print(f"正在播放: {song.display}")
        player = Player(args.player)
        player.play(url)
        if args.wait:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                player.stop()
        return 0

    if args.command == "lyric":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        if not songs:
            raise QQMusicError("没有搜索到歌曲")
        song = songs[min(args.index - 1, len(songs) - 1)]
        print(f"{song.display}\n")
        print(api.lyric(song.mid, auth.get("cookie", "")))
        return 0

    return 1



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ Music terminal client")
    parser.add_argument("--api-base", default=os.environ.get("QQMUSIC_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("QQMUSIC_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--player", default=os.environ.get("QQMUSIC_PLAYER"))
    parser.add_argument(
        "--platform",
        default=os.environ.get("MUSIC_PLATFORM", str(load_settings().get("platform", "qqmusic"))),
        choices=tuple(PLATFORMS.keys()),
        help="音乐平台",
    )

    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="扫码登录 QQ 音乐")
    login_parser.add_argument("qq_number", nargs="?", help="仅 --cookie 调试模式需要；普通登录不用填写")
    login_parser.add_argument("--cookie", help="直接保存网页登录 Cookie")
    login_parser.add_argument("--phone", help="网易云手机号验证码登录使用")
    login_parser.add_argument("--captcha", help="网易云手机验证码")
    login_parser.add_argument("--country-code", default="86", help="网易云手机号国家码")
    login_parser.add_argument("--send-captcha", action="store_true", help="只发送网易云手机验证码")
    login_parser.add_argument("--timeout-seconds", type=int, default=180, help="扫码登录超时时间")

    subparsers.add_parser("logout", help="删除本地登录信息")

    sync_parser = subparsers.add_parser("sync-playlists", help="同步并列出我的歌单")
    sync_parser.add_argument("qq_number", nargs="?", help="QQ 号；不传时使用已保存的登录信息")

    search_parser = subparsers.add_parser("search", help="搜索歌曲")
    search_parser.add_argument("keyword")
    search_parser.add_argument("-n", "--count", type=int, default=10)

    play_parser = subparsers.add_parser("play", help="搜索并播放歌曲")
    play_parser.add_argument("keyword")
    play_parser.add_argument("-n", "--count", type=int, default=10)
    play_parser.add_argument("-i", "--index", type=int, default=1, help="播放第几个搜索结果")
    play_parser.add_argument("-q", "--quality", default="320", choices=("128", "320", "flac"))
    play_parser.add_argument("--wait", action="store_true", help="前台等待，Ctrl+C 停止")

    lyric_parser = subparsers.add_parser("lyric", help="搜索并显示歌词")
    lyric_parser.add_argument("keyword")
    lyric_parser.add_argument("-n", "--count", type=int, default=10)
    lyric_parser.add_argument("-i", "--index", type=int, default=1, help="显示第几个搜索结果")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command:
            return run_cli(args)
        return run_tui(args)
    except QQMusicError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
