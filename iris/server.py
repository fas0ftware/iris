"""The ntfy-compatible endpoint, the inbox page and its API.

    POST /<topic>          publish (body = text; Title, Priority, Tags, Click headers) - like ntfy
    PUT  /<topic>          the same
    GET  /                 inbox page
    GET  /api/messages     recent messages (JSON), ?topic=
    GET  /api/status       counts for the last 24 hours
    GET  /api/tasks        tasks (JSON), ?state=open|snoozed|done|gone|all
    PUT  /api/tasks/<source>          a tool's whole current task list: {"tasks": [...]}
    POST /api/task/<key>/done|snooze|reopen   from the page (same origin) or an app (token)
    GET  /healthz          ok

Programs on this computer (loopback client and loopback Host) need no token. Anything else, such
as a phone on the home network, needs `Authorization: Bearer <token>` (ntfy clients send that for
an access token) or, for the page, a ?token= once (cookie); with no token set it is refused.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .core import MAX_BODY, Hub, message, summary, topic_ok
from .tasks import TaskError, validate

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

        def local(self) -> bool:
            """A program on this computer talking to this computer: the client is loopback and so
            is the Host it asked for. (A web page that rebinds its own name to 127.0.0.1 still
            sends its own name as Host, so it is not local.)"""
            try:
                client_ok = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                client_ok = False
            port = self.server.server_address[1]
            return client_ok and self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}",
                                                                   f"[::1]:{port}")

        def authorized(self) -> bool:
            """Local tools need no token (they never had one); everything else needs it, and
            without a token set nothing else gets in."""
            if self.local():
                return True
            if not token:
                return False
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
            if u.path == "/api/tasks":
                state = parse_qs(u.query).get("state", ["open"])[0]
                try:
                    return self.js(200, hub.tasks.list(time.time(), state))
                except TaskError as exc:
                    return self.js(400, {"ok": False, "error": str(exc)})
            if u.path == "/api/status":
                return self.js(200, {"version": __version__, **summary(hub.store, time.time()),
                                     "plan_at": hub.s.plan_at, "task_lang": hub.s.task_lang})
            return self.js(404, {"ok": False, "error": "not found"})

        def read_json(self):
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise TaskError("send JSON (Content-Type: application/json)")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise TaskError("bad length") from None
            if not 0 <= length <= 256 * 1024:
                raise TaskError("request too large")
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise TaskError("invalid JSON") from None

        def same_origin(self) -> bool:
            """The page's own buttons: the Origin must be this server as the browser reached it,
            and without a token only the loopback names count (DNS rebinding)."""
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            if not token and host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return False
            return self.headers.get("Origin") == f"http://{host}"

        def tasks_api(self, path: str):
            now = time.time()
            if path.startswith("/api/tasks/"):
                # A tool's list. Tools are not web pages: any Origin is refused (CSRF).
                if self.headers.get("Origin"):
                    return self.js(403, {"ok": False, "error": "cross-origin request refused"})
                source = path.removeprefix("/api/tasks/")
                if not topic_ok(source):
                    return self.js(400, {"ok": False, "error": "source: letters, digits, - and _ only"})
                try:
                    body = self.read_json()
                    items = validate(body.get("tasks") if isinstance(body, dict) else None)
                except TaskError as exc:
                    return self.js(400, {"ok": False, "error": str(exc)})
                result = hub.sync_tasks(source, items, now)
                wake.set()
                return self.js(200, {"ok": True, **result})
            parts = path.removeprefix("/api/task/").rsplit("/", 1)
            if len(parts) != 2 or parts[1] not in ("done", "snooze", "reopen"):
                return self.js(404, {"ok": False, "error": "not found"})
            if self.headers.get("Origin") and not self.same_origin():
                return self.js(403, {"ok": False, "error": "cross-origin request refused"})
            try:
                body = self.read_json()
                hours = body.get("hours", 24) if isinstance(body, dict) else 24
                if isinstance(hours, bool) or not isinstance(hours, (int, float)):
                    raise TaskError("hours must be a number")
                return self.js(200, {"ok": True, "task": hub.tasks.act(unquote(parts[0]), parts[1], now, float(hours))})
            except KeyError:
                return self.js(404, {"ok": False, "error": "no such task"})
            except TaskError as exc:
                return self.js(400, {"ok": False, "error": str(exc)})

        def do_POST(self):
            if not self.authorized():
                return self.js(401, {"ok": False, "error": "unauthorized"})
            path = urlparse(self.path).path
            if path.startswith(("/api/tasks/", "/api/task/")):
                return self.tasks_api(path)
            topic = path.strip("/")
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


def worker(hub: Hub, wake: threading.Event, stop: threading.Event, retry: float = 60,
           on_round=None) -> None:
    """Deliver right after a message arrives, retry failures every `retry` s, send the digest,
    and call `on_round` (writes the status file Olympos reads)."""
    while not stop.is_set():
        try:
            hub.digest(time.time())
            hub.plan(time.time())
            hub.deliver()
            hub.store.trim(time.time() - 90 * 86400)
            if on_round:
                on_round()
        except Exception as exc:  # keep serving
            print(f"(iris worker: {type(exc).__name__}: {exc})", flush=True)
        wake.wait(retry)
        wake.clear()


# token="" means no token: only allowed on loopback (the CLI refuses anything else without one).
def serve(hub: Hub, host: str, port: int, token: str = "", on_round=None) -> ThreadingHTTPServer:  # nosec B107
    wake, stop = threading.Event(), threading.Event()
    srv = ThreadingHTTPServer((host, port), make_handler(hub, token, wake))
    srv.daemon_threads = True
    t = threading.Thread(target=worker, args=(hub, wake, stop), kwargs={"on_round": on_round}, daemon=True)
    t.start()
    srv.iris_stop = stop  # type: ignore[attr-defined]
    srv.iris_wake = wake  # type: ignore[attr-defined]
    return srv
