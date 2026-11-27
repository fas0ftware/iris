"""What happens to a message: store it, decide, deliver (now or later), summarise what was held back."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - notify-send with an argument list, no shell
import time
import urllib.request
from datetime import datetime
from typing import Callable

from .config import PRIORITIES, Settings, in_quiet, priority, rank, valid_url

MAX_BODY = 4096
MAX_TITLE = 200


def clean(text: str, limit: int, newlines: bool = False) -> str:
    """Senders are our own tools, but their texts quote devices and attackers: no control
    characters (newlines only in bodies), capped."""
    out = []
    for c in text:
        if c == "\n" and newlines:
            out.append(c)
        elif c == "\t":
            out.append(" ")
        elif c.isprintable():
            out.append(c)
    return "".join(out)[:limit]


def topic_ok(topic: str) -> bool:
    return 0 < len(topic) <= 64 and all(c.isalnum() or c in "-_" for c in topic)


def message(topic: str, headers: dict, body: bytes, now: float) -> dict:
    """Build a message from an ntfy-style publish (headers are case-insensitive)."""
    h = {k.lower(): v for k, v in headers.items()}
    get = lambda *names: next((h[n] for n in names if h.get(n)), "")  # noqa: E731
    text = body[:MAX_BODY].decode("utf-8", "replace").strip()
    click = get("click", "x-click").strip()
    return {
        "ts": now,
        "topic": topic,
        "title": clean(get("title", "x-title", "t"), MAX_TITLE) or topic,
        "body": clean(text, MAX_BODY, newlines=True),
        "priority": priority(get("priority", "x-priority", "prio", "p")),
        "tags": clean(get("tags", "x-tags", "ta"), 120),
        "click": click if valid_url(click) else "",
    }


Output = Callable[[dict], None]   # raises on failure


def ntfy_output(url: str) -> Output:
    def send(m: dict) -> None:
        headers = {"Title": m["title"].encode("latin-1", "replace").decode("latin-1"), "Priority": m["priority"],
                   "Content-Type": "text/plain; charset=utf-8"}
        if m["tags"]:
            headers["Tags"] = m["tags"].encode("ascii", "ignore").decode()
        if m["click"]:
            headers["Click"] = m["click"]
        body = m["body"] + (f"\n(×{m['repeats'] + 1})" if m.get("repeats") else "")
        req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):  # nosec B310 - http(s) only, checked at load
            pass
    return send


def desktop_output() -> Output | None:
    exe = shutil.which("notify-send")
    if not exe:
        return None

    def send(m: dict) -> None:
        urgency = {"min": "low", "low": "low", "default": "normal", "high": "normal", "urgent": "critical"}[m["priority"]]
        # Notification servers may read the body as markup: escape it.
        body = m["body"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        subprocess.run([exe, "--app-name=Iris", f"--urgency={urgency}", "--", m["title"], body],  # nosec B603
                       check=True, timeout=10, capture_output=True)
    return send


class Hub:
    def __init__(self, settings: Settings, store, outputs: dict[str, Output], clock=time.time,
                 localtime=lambda ts: datetime.fromtimestamp(ts)):
        self.s = settings
        self.store = store
        self.outputs = outputs
        self.clock = clock
        self.localtime = localtime
        from .tasks import Tasks
        self.tasks = Tasks(store, settings.task_lang)

    def decide(self, m: dict) -> str:
        """The first matching rule wins (it may also change the priority); then the topic minimum,
        then quiet hours."""
        for r in self.s.rules:
            if r.applies(m):
                if r.priority:
                    m["priority"] = r.priority
                if r.action == "store":
                    return "below_min"
                if r.action == "hold":
                    return "quiet"
                if r.action == "always":
                    return "deliver"
                break
        if rank(m["priority"]) < rank(self.s.topic_min.get(m["topic"], "min")):
            return "below_min"
        hour = self.localtime(m["ts"]).hour
        if in_quiet(hour, self.s.quiet_start, self.s.quiet_end) and rank(m["priority"]) < rank(self.s.quiet_pass):
            return "quiet"
        return "deliver"

    def receive(self, m: dict) -> tuple[int, str]:
        """Store one message; returns (id, decision). A repeat within the dedup window is folded
        into the earlier message instead of being delivered again."""
        if self.s.dedup_minutes > 0:
            old = self.store.find_recent(m["topic"], m["title"], m["body"], m["ts"] - self.s.dedup_minutes * 60)
            if old:
                self.store.fold(old["id"], m["ts"])
                return old["id"], "duplicate"
        m["decision"] = self.decide(m)
        return self.store.add(m), m["decision"]

    def deliver(self) -> int:
        """Try every pending message on every output; a message counts as delivered when at least
        one output took it (the others are not retried, so nobody gets a message twice)."""
        done = 0
        for m in self.store.pending():
            took, failed = [], False
            for name, out in self.outputs.items():
                try:
                    out(m)
                    took.append(name)
                except Exception as exc:  # an output that fails must not stop the others
                    failed = True
                    print(f"(iris: {name} failed for message {m['id']}: {type(exc).__name__})")
            if took or not self.outputs:
                self.store.mark(m["id"], ",".join(took) or "nowhere", failed=False)
                done += 1
            else:
                self.store.mark(m["id"], "", failed=failed)
        return done

    def digest_due(self, now: float) -> bool:
        if not self.s.digest_at:
            return False
        h, mi = (int(x) for x in self.s.digest_at.split(":"))
        t = self.localtime(now)
        today = t.strftime("%Y-%m-%d")
        return (t.hour, t.minute) >= (h, mi) and self.store.get("digest_day") != today

    def digest(self, now: float, force: bool = False) -> dict | None:
        """One summary of the messages held back in quiet hours (stored as a message to deliver)."""
        if not force and not self.digest_due(now):
            return None
        self.store.put("digest_day", self.localtime(now).strftime("%Y-%m-%d"))
        held = self.store.held_back()
        if not held:
            return None
        per_topic: dict[str, int] = {}
        for m in held:
            per_topic[m["topic"]] = per_topic.get(m["topic"], 0) + 1 + m["repeats"]
        lines = [f"{n} from {t}" for t, n in sorted(per_topic.items(), key=lambda kv: -kv[1])]
        lines += [""] + [f"· {m['title']}: {m['body'][:120]}" for m in held[-8:]]
        if len(held) > 8:
            lines.append(f"· … and {len(held) - 8} more in the Iris inbox")
        d = {"ts": now, "topic": "iris", "title": f"Iris: {sum(per_topic.values())} message(s) held back overnight",
             "body": "\n".join(lines), "priority": "low", "tags": "sunrise", "click": "", "decision": "deliver"}
        d["id"] = self.store.add(d)
        self.store.mark_digested([m["id"] for m in held])
        return d


    def sync_tasks(self, source: str, items: list[dict], now: float) -> dict:
        """A tool's current task list; urgent new ones become messages right away."""
        result = self.tasks.sync(source, items, now)
        for t in result.pop("announce"):
            self.receive(self.tasks.announcement(t, now))
        return result

    def plan(self, now: float, force: bool = False) -> dict | None:
        """Once a day after plan_at: one message with every open task."""
        if not force and not self.tasks.plan_due(now, self.s.plan_at, self.localtime):
            return None
        self.tasks.mark_planned(now, self.localtime)
        m = self.tasks.plan(now)
        if m is None:
            return None
        m["decision"] = "deliver"     # asked for at this hour: quiet hours and minimums do not apply
        m["id"] = self.store.add(m)
        return m


def summary(store, now: float) -> dict:
    """Status for Olympos: what came in during the last 24 hours and what is stuck."""
    c = store.counts(now - 86400)
    stuck = len(store.pending())
    total = sum(v["messages"] + v["repeats"] for v in c.values())
    from .tasks import Tasks
    return {"messages_24h": total, "decisions": c, "undelivered": stuck,
            "priorities": list(PRIORITIES), "tasks": Tasks(store).counts()}
