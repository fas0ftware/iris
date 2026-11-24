"""The ntfy-compatible endpoint, the inbox page and its API.

    POST /<topic>          publish (body = text; Title, Priority, Tags, Click headers) - like ntfy
    PUT  /<topic>          the same
    GET  /                 inbox page
    GET  /api/messages     recent messages (JSON), ?topic=
    GET  /api/status       counts for the last 24 hours
    GET  /healthz          ok

Loopback only unless a token is set: then every request needs `Authorization: Bearer <token>`
(ntfy clients send that for an access token) or, for the page, a ?token= once (cookie).
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import __version__
from .core import MAX_BODY, Hub, message, summary, topic_ok

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
STATIC = {"/static/app.js": "text/javascript; charset=utf-8", "/static/style.css": "text/css; charset=utf-8",
          "/": "text/html; charset=utf-8"}


def asset(name: str) -> bytes:
    return resources.files("iris").joinpath("static", name).read_bytes()


def make_handler(hub: Hub, token: str, wake: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Iris/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the inbox is the log
            pass

        def send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            for k, v in {**HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def js(self, code: int, obj) -> None:
            self.send(code, json.dumps(obj).encode())

        def authorized(self) -> bool:
            if not token:
                return True
            given = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[7:]
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "iris_token":
                    given = given or v
            return hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self.js(200, {"ok": True})
            if u.path == "/" and token:
                q = parse_qs(u.query).get("token", [""])[0]
                if q and hmac.compare_digest(q.encode(), token.encode()):
                    return self.send(302, b"", "text/plain", {"Location": "/", "Set-Cookie":
                                     f"iris_token={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
            if u.path in STATIC and (u.path != "/" or self.authorized()):
                name = "index.html" if u.path == "/" else u.path.rsplit("/", 1)[1]
                return self.send(200, asset(name), STATIC[u.path])
            if not self.authorized():
                return self.js(401, {"ok": False, "error": "unauthorized"})
            if u.path == "/api/messages":
                q = parse_qs(u.query)
                topic = q.get("topic", [""])[0]
                if topic and not topic_ok(topic):
                    return self.js(400, {"ok": False, "error": "bad topic"})
                return self.js(200, hub.store.recent(200, topic))
            if u.path == "/api/status":
                return self.js(200, {"version": __version__, **summary(hub.store, time.time())})
            return self.js(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return self.js(401, {"ok": False, "error": "unauthorized"})
            topic = urlparse(self.path).path.strip("/")
            if not topic_ok(topic):
                return self.js(400, {"ok": False, "error": "topic: letters, digits, - and _ only"})
            if self.headers.get("Origin"):   # a web page must not post into the inbox (CSRF)
                return self.js(403, {"ok": False, "error": "cross-origin request refused"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.js(400, {"ok": False, "error": "bad length"})
            if length > MAX_BODY * 4:
                return self.js(413, {"ok": False, "error": "message too large"})
            body = self.rfile.read(length) if length > 0 else b""
            m = message(topic, dict(self.headers.items()), body, time.time())
            msg_id, decision = hub.receive(m)
            wake.set()
            # ntfy answers with the message; senders only look at the status code.
            return self.js(200, {"id": str(msg_id), "event": "message", "topic": topic, "decision": decision})

        do_PUT = do_POST

    return Handler


def worker(hub: Hub, wake: threading.Event, stop: threading.Event, retry: float = 60) -> None:
    """Deliver right after a message arrives, retry failures every `retry` s, send the digest."""
    while not stop.is_set():
        try:
            hub.digest(time.time())
            hub.deliver()
            hub.store.trim(time.time() - 90 * 86400)
        except Exception as exc:  # keep serving
            print(f"(iris worker: {type(exc).__name__}: {exc})")
        wake.wait(retry)
        wake.clear()


def serve(hub: Hub, host: str, port: int, token: str = "") -> ThreadingHTTPServer:
    wake, stop = threading.Event(), threading.Event()
    srv = ThreadingHTTPServer((host, port), make_handler(hub, token, wake))
    srv.daemon_threads = True
    t = threading.Thread(target=worker, args=(hub, wake, stop), daemon=True)
    t.start()
    srv.iris_stop = stop  # type: ignore[attr-defined]
    srv.iris_wake = wake  # type: ignore[attr-defined]
    return srv
