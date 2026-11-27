"""Tasks: things you have to do, not just things that happened.

A tool (Olympos, Chronos, ...) sends its *whole current list* of tasks for its source
(`PUT /api/tasks/<source>`). Iris keeps them:

- a task seen for the first time is **open**; urgent ones (priority high or urgent) are
  announced at once, the rest wait for the morning plan;
- a task the source no longer sends is **gone**: the problem went away, nobody has to tick it;
- you can mark a task **done** (if the source still reports it two hours later, it opens
  again: it was not really done) or **snooze** it for a while;
- once a day (`plan_at`) one message lists what is open: today's plan.

The same API (`GET /api/tasks`, `POST /api/task/<key>/done|snooze|reopen`) is what a phone
app talks to, with the Iris token.
"""

from __future__ import annotations

import re
from datetime import datetime

from .config import PRIORITIES, rank, valid_url
from .core import clean

ITEM_ID = re.compile(r"[a-z0-9][a-z0-9:_.\-]{0,79}\Z")
MAX_TASKS = 100
REOPEN_AFTER = 2 * 3600          # done, but still reported this much later: open again
SNOOZE_MAX_HOURS = 24 * 30
STATES = ("open", "snoozed", "done", "gone")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    key          TEXT PRIMARY KEY,          -- source:item
    source       TEXT NOT NULL,
    item         TEXT NOT NULL,
    title        TEXT NOT NULL,
    action       TEXT NOT NULL DEFAULT '',  -- what to do, in words
    command      TEXT NOT NULL DEFAULT '',  -- the command to run, if there is one
    why          TEXT NOT NULL DEFAULT '',
    link         TEXT NOT NULL DEFAULT '',
    priority     TEXT NOT NULL,
    state        TEXT NOT NULL,             -- open | snoozed | done | gone
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    updated      REAL NOT NULL,
    snooze_until REAL NOT NULL DEFAULT 0,
    done_at      REAL NOT NULL DEFAULT 0,
    reopened     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks (source, state);
"""
COLUMNS = ("key", "source", "item", "title", "action", "command", "why", "link", "priority", "state",
           "first_seen", "last_seen", "updated", "snooze_until", "done_at", "reopened")

TEXT = {
    "en": {"todo": "To do", "plan": "Today's plan: {n} task(s)", "more": "… and {n} more in the Iris inbox",
           "again": " (still there)", "run": "run"},
    "tr": {"todo": "Yapılacak", "plan": "Bugünün işleri: {n}", "more": "… ve {n} iş daha (Iris sayfasında)",
           "again": " (hâlâ duruyor)", "run": "çalıştır"},
}


class TaskError(ValueError):
    pass


def validate(items) -> list[dict]:
    """The list a tool sent, checked and cleaned. Raises TaskError."""
    if not isinstance(items, list):
        raise TaskError("tasks must be a list")
    if len(items) > MAX_TASKS:
        raise TaskError(f"at most {MAX_TASKS} tasks per source")
    out, seen = [], set()
    for i, t in enumerate(items):
        if not isinstance(t, dict):
            raise TaskError(f"tasks[{i}] must be an object")
        item = str(t.get("id", ""))
        if not ITEM_ID.match(item):
            raise TaskError(f"tasks[{i}].id: lowercase letters, digits and :_.- only (max 80)")
        if item in seen:
            raise TaskError(f"tasks[{i}].id repeats {item}")
        seen.add(item)
        title = clean(str(t.get("title", "")), 200)
        if not title:
            raise TaskError(f"tasks[{i}].title is required")
        prio = str(t.get("priority", "default"))
        if prio not in PRIORITIES:
            raise TaskError(f"tasks[{i}].priority must be one of " + ", ".join(PRIORITIES))
        link = str(t.get("link", ""))
        out.append({"item": item, "title": title,
                    "action": clean(str(t.get("action", "")), 1000, newlines=True),
                    "command": clean(str(t.get("command", "")), 500),
                    "why": clean(str(t.get("why", "")), 500, newlines=True),
                    "link": link if valid_url(link) else "", "priority": prio})
    return out


class Tasks:
    def __init__(self, store, lang: str = "en"):
        self.store = store
        self.lang = lang if lang in TEXT else "en"
        with store.lock, store.conn:
            store.conn.executescript(SCHEMA)

    def t(self, key: str, **kw) -> str:
        return TEXT[self.lang][key].format(**kw)

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self.store.lock:
            return [dict(zip(COLUMNS, r)) for r in self.store.conn.execute(sql, args).fetchall()]

    def get(self, key: str) -> dict | None:
        r = self._rows(f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE key = ?", (key,))  # nosec B608 - fixed columns
        return r[0] if r else None

    def sync(self, source: str, items: list[dict], now: float) -> dict:
        """Apply a source's full list. Returns counts and the tasks to announce now."""
        current = {t["key"]: t for t in self._rows(
            f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE source = ?", (source,))}  # nosec B608
        announce, counts = [], {"new": 0, "reopened": 0, "gone": 0, "kept": 0}
        with self.store.lock, self.store.conn as c:
            for t in items:
                key = f"{source}:{t['item']}"
                old = current.pop(key, None)
                fields = (t["title"], t["action"], t["command"], t["why"], t["link"], t["priority"])
                if old is None:
                    c.execute("INSERT INTO tasks (key, source, item, title, action, command, why, link, priority, "
                              "state, first_seen, last_seen, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                              (key, source, t["item"], *fields, now, now, now))
                    counts["new"] += 1
                    if rank(t["priority"]) >= rank("high"):
                        announce.append({**t, "key": key, "reopened": 0})
                    continue
                state, reopened = old["state"], old["reopened"]
                if state == "gone" or (state == "done" and now - old["done_at"] >= REOPEN_AFTER):
                    reopened += 1 if state == "done" else 0
                    state = "open"
                    counts["reopened"] += 1
                    if rank(t["priority"]) >= rank("high"):
                        announce.append({**t, "key": key, "reopened": reopened})
                elif state == "snoozed" and old["snooze_until"] <= now:
                    state = "open"
                else:
                    counts["kept"] += 1
                c.execute("UPDATE tasks SET title = ?, action = ?, command = ?, why = ?, link = ?, priority = ?, "
                          "state = ?, reopened = ?, last_seen = ?, updated = ? WHERE key = ?",
                          (*fields, state, reopened, now, now if state != old["state"] else old["updated"], key))
            for key, old in current.items():
                if old["state"] != "gone":
                    c.execute("UPDATE tasks SET state = 'gone', updated = ? WHERE key = ?", (now, key))
                    counts["gone"] += 1
        return {**counts, "announce": announce}

    def wake(self, now: float) -> None:
        """Snoozes that ran out are open again."""
        self.store._q("UPDATE tasks SET state = 'open', updated = ? WHERE state = 'snoozed' AND snooze_until <= ?",
                      (now, now))

    def list(self, now: float, state: str = "open") -> list[dict]:
        self.wake(now)
        if state == "all":
            rows = self._rows(f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE state != 'gone' OR updated >= ?",  # nosec B608
                              (now - 7 * 86400,))
        else:
            if state not in STATES:
                raise TaskError("state must be open, snoozed, done, gone or all")
            rows = self._rows(f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE state = ?", (state,))  # nosec B608
        order = {s: i for i, s in enumerate(STATES)}
        rows.sort(key=lambda t: (order[t["state"]], -rank(t["priority"]), t["first_seen"]))
        return rows

    def act(self, key: str, what: str, now: float, hours: float = 24) -> dict:
        task = self.get(key)
        if task is None:
            raise KeyError(key)
        if what == "done":
            self.store._q("UPDATE tasks SET state = 'done', done_at = ?, updated = ? WHERE key = ?", (now, now, key))
        elif what == "snooze":
            if not 0 < hours <= SNOOZE_MAX_HOURS:
                raise TaskError(f"snooze between 0 and {SNOOZE_MAX_HOURS} hours")
            self.store._q("UPDATE tasks SET state = 'snoozed', snooze_until = ?, updated = ? WHERE key = ?",
                          (now + hours * 3600, now, key))
        elif what == "reopen":
            self.store._q("UPDATE tasks SET state = 'open', updated = ? WHERE key = ?", (now, key))
        else:
            raise TaskError("action must be done, snooze or reopen")
        return self.get(key) or {}

    def counts(self) -> dict:
        with self.store.lock:
            rows = self.store.conn.execute("SELECT state, priority, COUNT(*) FROM tasks GROUP BY state, priority").fetchall()
        out = {s: 0 for s in STATES}
        urgent = 0
        for state, prio, n in rows:
            out[state] += n
            if state == "open" and rank(prio) >= rank("high"):
                urgent += n
        return {**out, "open_urgent": urgent}

    # --- messages ---------------------------------------------------------------------

    def announcement(self, t: dict, now: float) -> dict:
        lines = [t["action"]] if t["action"] else []
        if t["command"]:
            lines.append(f"$ {t['command']}")
        title = f"{self.t('todo')}: {t['title']}" + (self.t("again") if t.get("reopened") else "")
        return {"ts": now, "topic": "tasks", "title": title[:200], "body": "\n".join(lines)[:4096],
                "priority": t["priority"], "tags": "memo", "click": t.get("link", "")}

    def plan(self, now: float) -> dict | None:
        """Today's plan as one message, or None when nothing is open."""
        open_ = self.list(now, "open")
        if not open_:
            return None
        lines = []
        for i, t in enumerate(open_[:10], 1):
            mark = "!" if rank(t["priority"]) >= rank("high") else ("·" if t["priority"] in ("low", "min") else "")
            lines.append(f"{i}. {mark + ' ' if mark else ''}{t['title']}" + (self.t("again") if t["reopened"] else ""))
            if t["command"]:
                lines.append(f"   $ {t['command']}")
        if len(open_) > 10:
            lines.append(self.t("more", n=len(open_) - 10))
        top = max(rank(t["priority"]) for t in open_)
        return {"ts": now, "topic": "tasks", "title": self.t("plan", n=len(open_)), "body": "\n".join(lines),
                "priority": "high" if top >= rank("urgent") else "default", "tags": "clipboard", "click": ""}

    def plan_due(self, now: float, at: str, localtime=datetime.fromtimestamp) -> bool:
        if not at:
            return False
        h, m = (int(x) for x in at.split(":"))
        t = localtime(now)
        return (t.hour, t.minute) >= (h, m) and self.store.get("plan_day") != t.strftime("%Y-%m-%d")

    def mark_planned(self, now: float, localtime=datetime.fromtimestamp) -> None:
        self.store.put("plan_day", localtime(now).strftime("%Y-%m-%d"))
