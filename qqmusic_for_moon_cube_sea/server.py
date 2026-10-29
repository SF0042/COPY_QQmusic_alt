#!/usr/bin/env python3
"""月下方海 · local server.

The browser client from ``qqmusic_for_web_browser`` with its front end replaced by 月下方海 (the cube sea).
The backend is reused as it is: this file imports ``qqmusic_for_web_browser/server.py`` and serves its API
(login, playlists, songs, lyrics, ...) unchanged, and adds two routes the cube sea needs:

* ``/api/image``   album covers, relayed from the music platforms' image hosts, so the page may read their
                   colours (cover art on the cube, scene tint)
* ``/api/stream``  song audio with HTTP Range passed through, so the page can seek and analyse the beat

Login and settings are kept in this folder (``.qqmusic_auth.json``, ``.netease_auth.json``,
``.qqmusic_settings.json``), separate from the other clients.

Run::

    cd qqmusic_for_moon_cube_sea
    python3 server.py

then open http://127.0.0.1:8765 . Nothing here bypasses membership, DRM, region or payment limits: a song the
account may not play simply fails, and the page moves on to the next one.
"""

from __future__ import annotations

import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE.parent / "qqmusic_for_web_browser"
STATIC_DIR = HERE / "static"

sys.path.insert(0, str(WEB_DIR))
import server as web  # noqa: E402  (the browser client's backend, used as a library)

# This client keeps its own login and settings files (the backend reads these paths when it needs them)
os.environ["QQMUSIC_AUTH_FILE"] = str(HERE / ".qqmusic_auth.json")
os.environ["NETEASE_AUTH_FILE"] = str(HERE / ".netease_auth.json")
os.environ["QQMUSIC_SETTINGS_FILE"] = str(HERE / ".qqmusic_settings.json")
web.settings_path = lambda: HERE / ".qqmusic_settings.json"

# Hosts whose covers may be relayed (QQ Music and NetEase image CDNs)
IMAGE_HOSTS = re.compile(r"^https?://([a-z0-9-]+\.)*(qq\.com|qpic\.cn|gtimg\.cn|gtimg\.com|126\.net|163\.com)(:\d+)?/", re.I)

# The page asks for the same song more than once (playback, seeking, beat analysis); keep its URL briefly
_url_cache: dict[tuple[str, str, str, str], tuple[str, float]] = {}
_url_lock = threading.Lock()
URL_TTL = 8 * 60


def resolve_song_url(mid: str, media_mid: str, quality: str) -> str:
    key = (web.current_platform(), mid, media_mid, quality)
    now = time.time()
    with _url_lock:
        hit = _url_cache.get(key)
        if hit and now - hit[1] < URL_TTL:
            return hit[0]
    url = web.current_api().song_url(mid, quality, web.require_cookie(), media_mid)
    if not url:
        raise web.QQMusicError("这首歌没有可用的播放地址")
    with _url_lock:
        _url_cache[key] = (url, now)
    return url


class Handler(web.Handler):
    server_version = "MoonCubeSea/1.0"

    def do_GET(self) -> None:  # noqa: N802
        path, query = self.parsed_url()
        try:
            if path == "/api/image":
                self.relay_image(query)
                return
            if path == "/api/stream":
                self.stream_audio(query)
                return
        except (BrokenPipeError, ConnectionResetError):
            return  # the browser dropped the request (a seek or a skip)
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(exc, 500)
            return
        super().do_GET()

    # the cube sea instead of the original player
    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def relay_image(self, query: dict[str, list[str]]) -> None:
        url = self.first_query(query, "url")
        if url.startswith("//"):
            url = "https:" + url
        if not IMAGE_HOSTS.match(url):
            raise web.QQMusicError("不支持的封面地址")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"})
        with urllib.request.urlopen(request, timeout=web.api_timeout) as response:
            body = response.read(8 * 1024 * 1024)
            content_type = response.headers.get("Content-Type", "image/jpeg")
        if not content_type.startswith("image/"):
            raise web.QQMusicError("封面地址返回的不是图片")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def stream_audio(self, query: dict[str, list[str]]) -> None:
        mid = self.first_query(query, "mid")
        media_mid = self.first_query(query, "media_mid")
        quality = self.first_query(query, "quality", "320")
        if not mid:
            raise web.QQMusicError("缺少歌曲 mid")
        url = resolve_song_url(mid, media_mid, quality)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}
        wanted = self.headers.get("Range")
        if wanted:
            headers["Range"] = wanted
        try:
            response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=web.api_timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                with _url_lock:  # the signed URL may have expired; the next request resolves it again
                    _url_cache.pop((web.current_platform(), mid, media_mid, quality), None)
            raise
        with response:
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type", "audio/mpeg"))
            for name in ("Content-Length", "Content-Range"):
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def main() -> int:
    host = os.environ.get("QQMUSIC_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("QQMUSIC_WEB_PORT", "8765"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"月下方海: http://{shown}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
