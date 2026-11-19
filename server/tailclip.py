#!/usr/bin/env python3
"""tailclip: a tiny, private clipboard relay for your Tailscale tailnet.

Clips (text, images, files) are kept in memory only, capped in number and
size, and expire after a TTL. Standard library only, so it runs anywhere
Python 3.9+ runs.

Run behind `tailscale serve` (recommended, see README), either on a Unix
socket (Linux/macOS) or on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent

INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_PREVIEW_CHARS = 2000
CLIP_PATH = re.compile(r"^/api/clips/([0-9a-f]{32})$")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass
class Config:
    max_items: int = 20
    max_item_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 150 * 1024 * 1024
    ttl_seconds: int = 24 * 3600
    allowed_logins: frozenset = frozenset()

    @classmethod
    def from_env(cls) -> "Config":
        logins = os.environ.get("TAILCLIP_ALLOWED_LOGINS", "")
        return cls(
            max_items=_env_int("TAILCLIP_MAX_ITEMS", cls.max_items),
            max_item_bytes=_env_int("TAILCLIP_MAX_ITEM_MB", 25) * 1024 * 1024,
            max_total_bytes=_env_int("TAILCLIP_MAX_TOTAL_MB", 150) * 1024 * 1024,
            ttl_seconds=_env_int("TAILCLIP_TTL_HOURS", 24) * 3600,
            allowed_logins=frozenset(
                x.strip().lower() for x in logins.split(",") if x.strip()
            ),
        )


@dataclass
class Clip:
    kind: str  # "text" | "image" | "file"
    mime: str
    data: bytes
    name: Optional[str] = None
    source: str = "Unknown"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)

    def meta(self) -> dict:
        out = {
            "id": self.id,
            "kind": self.kind,
            "mime": self.mime,
            "name": self.name,
            "size": len(self.data),
            "created": self.created,
            "source": self.source,
        }
        if self.kind == "text":
            text = self.data.decode("utf-8", errors="replace")
            out["preview"] = text[:TEXT_PREVIEW_CHARS]
            out["truncated"] = len(text) > TEXT_PREVIEW_CHARS
            out["chars"] = len(text)
        return out


class Store:
    """Newest-first, in-memory list of clips with count, size and age limits."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._clips: list[Clip] = []
        self._lock = threading.Lock()
        self.version = 0

    def _prune(self) -> None:
        before = len(self._clips)
        cutoff = time.time() - self.cfg.ttl_seconds
        self._clips = [c for c in self._clips if c.created >= cutoff]
        while len(self._clips) > self.cfg.max_items:
            self._clips.pop()
        while self._clips and sum(len(c.data) for c in self._clips) > self.cfg.max_total_bytes:
            self._clips.pop()
        if len(self._clips) != before:
            self.version += 1

    def prune(self) -> None:
        with self._lock:
            self._prune()

    def add(self, clip: Clip) -> Clip:
        with self._lock:
            self._clips.insert(0, clip)
            self.version += 1
            self._prune()
        return clip

    def snapshot(self) -> tuple[int, list[Clip]]:
        with self._lock:
            self._prune()
            return self.version, list(self._clips)

    def latest(self) -> Optional[Clip]:
        with self._lock:
            self._prune()
            return self._clips[0] if self._clips else None

    def get(self, clip_id: str) -> Optional[Clip]:
        with self._lock:
            self._prune()
            return next((c for c in self._clips if c.id == clip_id), None)

    def delete(self, clip_id: str) -> bool:
        with self._lock:
            n = len(self._clips)
            self._clips = [c for c in self._clips if c.id != clip_id]
            if len(self._clips) != n:
                self.version += 1
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._clips.clear()
            self.version += 1


def sniff_image(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    return None


def classify(body: bytes, content_type: str, filename: Optional[str]) -> tuple[str, str]:
    """Return (kind, mime) for an uploaded body."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    image = sniff_image(body)
    if image:
        return "image", image
    if ctype.startswith("text/") and not filename:
        return "text", "text/plain; charset=utf-8"
    if not filename and ctype in ("", "application/octet-stream", "application/x-www-form-urlencoded"):
        try:
            body.decode("utf-8")
            return "text", "text/plain; charset=utf-8"
        except UnicodeDecodeError:
            pass
    if ctype and "/" in ctype and ctype not in ("image/svg+xml", "text/html"):
        return "file", ctype
    return "file", "application/octet-stream"


def clean_label(value: str, limit: int) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
    return value[:limit]


def clean_filename(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    name = unquote(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = clean_label(name, 200).strip(". ")
    return name or None


def guess_source(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for needle, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("windows", "Windows"),
        ("macintosh", "Mac"),
        ("mac os", "Mac"),
        ("cros", "ChromeOS"),
        ("linux", "Linux"),
        ("darwin", "Apple device"),
        ("curl", "curl"),
    ):
        if needle in ua:
            return label
    return "Unknown"


class Handler(BaseHTTPRequestHandler):
    server_version = f"tailclip/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def address_string(self) -> str:
        ca = self.client_address
        return ca[0] if isinstance(ca, tuple) and ca else "unix"

    def log_message(self, fmt: str, *args) -> None:
        # Quiet by default: clips are private, and so is who sent what when.
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _login(self) -> Optional[str]:
        return self.headers.get("Tailscale-User-Login") or None

    def _allowed(self) -> bool:
        if not self.cfg.allowed_logins:
            return True
        login = (self._login() or "").lower()
        if login in self.cfg.allowed_logins:
            return True
        self._error(HTTPStatus.FORBIDDEN, "This Tailscale login is not allowed.")
        return False

    def _same_origin(self) -> bool:
        """Block cross-site writes from browsers (a web page must not be able
        to plant something in your clipboard). Non-browser clients such as
        curl, PowerShell or Apple Shortcuts send no Origin and are allowed."""
        site = self.headers.get("Sec-Fetch-Site")
        if site:
            ok = site in ("same-origin", "none")
        else:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}
            ok = urlsplit(origin).netloc in hosts
        if not ok:
            self._error(HTTPStatus.FORBIDDEN, "Cross-site request blocked.")
        return ok

    def _read_body(self) -> Optional[bytes]:
        limit = self.cfg.max_item_bytes
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(1024).strip()
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline(1024)
                    break
                total += size
                if total > limit:
                    self.close_connection = True
                    self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Clip is too large.")
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(1024)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required.")
            return None
        length = int(length)
        if length > limit:
            self.close_connection = True
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Clip is too large.")
            return None
        return self.rfile.read(length)

    def _send_clip(self, clip: Clip, download: bool = False) -> None:
        headers = {
            "X-Tailclip-Id": clip.id,
            "X-Tailclip-Kind": clip.kind,
            "Content-Security-Policy": "sandbox",
        }
        if clip.name:
            headers["X-Tailclip-Name"] = quote(clip.name)
        inline = clip.kind == "text" or clip.mime in INLINE_IMAGE_TYPES
        if download or not inline:
            name = clip.name or ("clip.txt" if clip.kind == "text" else "clip")
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
        mime = clip.mime if (inline or clip.kind != "text") else "text/plain; charset=utf-8"
        self._send(HTTPStatus.OK, clip.data, mime, headers)

    # -- routes -----------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._allowed():
            return
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, self.server.index_html, "text/html; charset=utf-8", {  # type: ignore[attr-defined]
                "Content-Security-Policy": (
                    "default-src 'none'; img-src 'self' blob: data:; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                    "form-action 'none'; frame-ancestors 'none'"
                ),
            })
        elif path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True, "version": VERSION})
        elif path == "/api/clips":
            version, clips = self.store.snapshot()
            self._json(HTTPStatus.OK, {
                "version": version,
                "clips": [c.meta() for c in clips],
                "user": self._login(),
                "limits": {
                    "max_items": self.cfg.max_items,
                    "max_item_bytes": self.cfg.max_item_bytes,
                    "ttl_seconds": self.cfg.ttl_seconds,
                },
            })
        elif path == "/api/latest":
            clip = self.store.latest()
            if clip is None:
                self._send(HTTPStatus.NOT_FOUND, b"tailclip is empty", "text/plain; charset=utf-8")
            else:
                self._send_clip(clip)
        elif (m := CLIP_PATH.match(path)):
            clip = self.store.get(m.group(1))
            if clip is None:
                self._error(HTTPStatus.NOT_FOUND, "No such clip (it may have expired).")
            else:
                self._send_clip(clip, download="download" in url.query)
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        if not (self._allowed() and self._same_origin()):
            return
        if urlsplit(self.path).path != "/api/clips":
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        body = self._read_body()
        if body is None:
            return
        name = clean_filename(self.headers.get("X-Filename"))
        kind, mime = classify(body, self.headers.get("Content-Type", ""), name)
        if kind == "text":
            text = body.decode("utf-8", errors="replace")
            if not text.strip():
                self._error(HTTPStatus.BAD_REQUEST, "Nothing to send: the clip is empty.")
                return
            body = text.encode("utf-8")
            name = None
        elif not body:
            self._error(HTTPStatus.BAD_REQUEST, "Nothing to send: the clip is empty.")
            return
        source = clean_label(self.headers.get("X-Source", ""), 40) or guess_source(
            self.headers.get("User-Agent", ""))
        clip = self.store.add(Clip(kind=kind, mime=mime, data=body, name=name, source=source))
        self._json(HTTPStatus.CREATED, clip.meta())

    def do_DELETE(self) -> None:
        if not (self._allowed() and self._same_origin()):
            return
        path = urlsplit(self.path).path
        if path == "/api/clips":
            self.store.clear()
            self._json(HTTPStatus.OK, {"ok": True})
        elif (m := CLIP_PATH.match(path)):
            if self.store.delete(m.group(1)):
                self._json(HTTPStatus.OK, {"ok": True})
            else:
                self._error(HTTPStatus.NOT_FOUND, "No such clip.")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")


class QuietErrorsMixin:
    """Clients (browsers, phones going to sleep) drop connections all the
    time; that is not worth a traceback."""

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)  # type: ignore[misc]


class TCPHTTPServer(QuietErrorsMixin, ThreadingHTTPServer):
    pass


if hasattr(socket, "AF_UNIX"):
    class UnixHTTPServer(QuietErrorsMixin, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8790,
                unix_path: Optional[str] = None, verbose: bool = False):
    if unix_path:
        if not hasattr(socket, "AF_UNIX"):
            raise SystemExit("Unix sockets are not supported on this platform; use --port.")
        if os.path.exists(unix_path):
            os.unlink(unix_path)
        old = os.umask(0o077)
        try:
            server = UnixHTTPServer(unix_path, Handler)
        finally:
            os.umask(old)
    else:
        server = TCPHTTPServer((host, port), Handler)
    server.store = Store(cfg)
    server.cfg = cfg
    server.verbose = verbose
    server.index_html = (HERE / "index.html").read_bytes()

    def janitor() -> None:
        while True:
            time.sleep(60)
            server.store.prune()

    threading.Thread(target=janitor, daemon=True).start()
    return server


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="tailclip: private clipboard relay for your tailnet")
    parser.add_argument("--host", default="127.0.0.1", help="TCP address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8790, help="TCP port (default 8790)")
    parser.add_argument("--unix", metavar="PATH", help="listen on a Unix socket instead of TCP")
    parser.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    server = make_server(cfg, args.host, args.port, args.unix, args.verbose)
    where = f"unix:{args.unix}" if args.unix else f"http://{args.host}:{args.port}"
    print(f"tailclip {VERSION} listening on {where}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if args.unix and os.path.exists(args.unix):
            os.unlink(args.unix)


if __name__ == "__main__":
    main()
