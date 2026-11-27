"""iris: one inbox for your tools' notifications.

    iris serve                 listen on 127.0.0.1:8090 (ntfy-compatible), deliver, send the morning digest,
                               and keep ~/.local/state/iris/status.json current for Olympos
    iris send TOPIC "text"     publish a message (to test the rules)
    iris inbox [--topic T]     print recent messages
    iris status [--save]       counts for the last 24 h; --save writes the file Olympos reads
    iris tasks [--all]         what is open (tasks that Olympos, Chronos, ... sent)
    iris task KEY done|snooze|reopen [--hours H]
    iris plan [--send]         today's plan now (--send delivers it)
    iris pair [--new]          the code a phone app pairs with (makes the token once)
    iris demo                  fill a separate database with fictional messages
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config
from .config import PRIORITIES, ConfigError
from .core import Hub, desktop_output, message, ntfy_output, summary, topic_ok
from .store import Store


def outputs(s: config.Settings) -> dict:
    out = {}
    if s.ntfy:
        out["ntfy"] = ntfy_output(s.ntfy)
    if s.desktop:
        d = desktop_output()
        if d:
            out["desktop"] = d
    return out


def save_status(store: Store, path: Path) -> None:
    data = {"version": __version__, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **summary(store, time.time()),
            "recent": [{k: m[k] for k in ("ts", "topic", "title", "priority", "decision", "repeats")}
                       for m in store.recent(10)]}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def lan_address() -> str:
    """This computer's address on the network with the default route (no packet is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="iris", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"iris {__version__}")
    p.add_argument("--config", type=Path, default=None, help=f"default {config.default_path()}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None, help="default: [server] host, else 127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    sd = sub.add_parser("send")
    sd.add_argument("topic")
    sd.add_argument("text")
    sd.add_argument("--title", default="")
    sd.add_argument("--priority", choices=PRIORITIES, default="default")
    ib = sub.add_parser("inbox")
    ib.add_argument("--topic", default="")
    ib.add_argument("-n", type=int, default=20)
    st = sub.add_parser("status")
    st.add_argument("--save", nargs="?", const="", default=None, metavar="PATH")
    pr = sub.add_parser("pair")
    pr.add_argument("--new", action="store_true", help="make a new token (paired phones must pair again)")
    tl = sub.add_parser("tasks")
    tl.add_argument("--all", action="store_true", help="also snoozed, done and recently gone")
    ta = sub.add_parser("task")
    ta.add_argument("key")
    ta.add_argument("what", choices=("done", "snooze", "reopen"))
    ta.add_argument("--hours", type=float, default=24)
    pl = sub.add_parser("plan")
    pl.add_argument("--send", action="store_true")
    dm = sub.add_parser("demo")
    dm.add_argument("--db", default="iris-demo.db")
    args = p.parse_args(argv)

    try:
        s = config.load(args.config)
    except ConfigError as exc:
        print(f"iris: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "demo":
        from .demo import fill
        store = Store(args.db)
        fill(store)
        print(f"Fictional messages written to {args.db}. Try: IRIS_DB={args.db} python -m iris serve --port 8091")
        return 0

    db = os.environ.get("IRIS_DB") or str(s.db)
    store = Store(db)
    hub = Hub(s, store, outputs(s))

    if args.cmd == "serve":
        token = config.read_token()
        args.host = args.host or s.host
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = args.host == "localhost"
        if not loopback and len(token) < 16:
            print("iris: listening beyond this machine needs a token (16+ characters): run iris pair", file=sys.stderr)
            return 2
        from .server import serve
        status_file = config.state_dir() / "status.json"
        srv = serve(hub, args.host, args.port, token, on_round=lambda: save_status(store, status_file))
        outs = ", ".join(hub.outputs) or "nowhere (store only)"
        print(f"Iris {__version__} on http://{args.host}:{args.port}/ · delivering to {outs}"
              + (f" · quiet {s.quiet_start:02d}-{s.quiet_end:02d}" if s.quiet_start is not None else ""), flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.iris_stop.set()  # type: ignore[attr-defined]
            srv.iris_wake.set()  # type: ignore[attr-defined]
        return 0

    if args.cmd == "send":
        if not topic_ok(args.topic):
            print("iris: topic: letters, digits, - and _ only", file=sys.stderr)
            return 2
        m = message(args.topic, {"Title": args.title, "Priority": args.priority}, args.text.encode(), time.time())
        msg_id, decision = hub.receive(m)
        hub.deliver()
        print(f"message {msg_id}: {decision}")
        return 0

    if args.cmd == "inbox":
        for m in reversed(store.recent(args.n, args.topic)):
            rep = f" ×{m['repeats'] + 1}" if m["repeats"] else ""
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['last_ts']))}  {m['priority']:<7} {m['topic']:<10} "
                  f"{m['title']}{rep}  [{m['decision']}{' → ' + m['delivered'] if m['delivered'] else ''}]")
        return 0

    if args.cmd == "pair":
        import secrets
        path = config.token_path()
        token = config.read_token()
        if args.new or not token:
            if os.environ.get("IRIS_TOKEN"):
                print("iris: IRIS_TOKEN is set in the environment; change it there", file=sys.stderr)
                return 2
            token = secrets.token_urlsafe(24)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
            print(f"New token in {path}. Restart Iris: systemctl --user restart iris.service")
        ip = lan_address()
        print(f"Pairing code (paste it into the app; it holds the token, keep it private):\n\n  http://{ip}:8090#{token}\n")
        if s.host in ("127.0.0.1", "localhost", "::1"):
            print("Iris listens on this computer only. To let the phone in, add to the settings and restart:\n"
                  "  [server]\n  host = \"0.0.0.0\"\n")
        net = ".".join(ip.split(".")[:3]) + ".0/24" if ip.count(".") == 3 else "your-network/24"
        print(f"Allow only the home network through the firewall:\n  sudo ufw allow from {net} to any port 8090 proto tcp")
        return 0

    if args.cmd == "tasks":
        rows = hub.tasks.list(time.time(), "all" if args.all else "open")
        for t in rows:
            state = "" if t["state"] == "open" else f"[{t['state']}] "
            print(f"{t['priority']:<7} {state}{t['title']}   ({t['key']})")
            if t["command"]:
                print(f"        $ {t['command']}")
        if not rows:
            print("nothing open")
        return 0

    if args.cmd == "task":
        from .tasks import TaskError
        try:
            t = hub.tasks.act(args.key, args.what, time.time(), args.hours)
        except KeyError:
            print(f"iris: no task {args.key} (see: iris tasks --all)", file=sys.stderr)
            return 1
        except TaskError as exc:
            print(f"iris: {exc}", file=sys.stderr)
            return 2
        print(f"{t['key']}: {t['state']}")
        return 0

    if args.cmd == "plan":
        if args.send:
            m = hub.plan(time.time(), force=True)
            hub.deliver()
        else:
            m = hub.tasks.plan(time.time())
        print(f"{m['title']}\n{m['body']}" if m else "nothing open")
        return 0

    if args.cmd == "status":
        print(json.dumps({"version": __version__, **summary(store, time.time())}, indent=2))
        if args.save is not None:
            save_status(store, Path(args.save) if args.save else config.state_dir() / "status.json")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
