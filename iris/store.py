"""SQLite: every message that came in, what happened to it, and the delivery queue."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    topic     TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL,
    priority  TEXT NOT NULL,
    tags      TEXT NOT NULL DEFAULT '',
    click     TEXT NOT NULL DEFAULT '',
    repeats   INTEGER NOT NULL DEFAULT 0,      -- identical messages folded into this one
    last_ts   REAL NOT NULL,
    decision  TEXT NOT NULL,                   -- deliver | quiet | below_min | duplicate
    delivered TEXT NOT NULL DEFAULT '',        -- outputs that took it: "ntfy,desktop"
    attempts  INTEGER NOT NULL DEFAULT 0,
    digested  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS idx_messages_key ON messages (topic, title, last_ts);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

COLUMNS = ("id", "ts", "topic", "title", "body", "priority", "tags", "click", "repeats", "last_ts",
           "decision", "delivered", "attempts", "digested")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.Lock()   # one writer at a time: the server is threaded
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.path.exists(self.path):
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)

    def _q(self, sql: str, args=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, args)

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(zip(COLUMNS, r)) for r in self.conn.execute(sql, args).fetchall()]

    def add(self, m: dict) -> int:
        cur = self._q("INSERT INTO messages (ts, topic, title, body, priority, tags, click, last_ts, decision) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (m["ts"], m["topic"], m["title"], m["body"], m["priority"], m["tags"], m["click"], m["ts"],
                       m["decision"]))
        return int(cur.lastrowid)

    def find_recent(self, topic: str, title: str, body: str, since: float) -> dict | None:
        r = self.rows(f"SELECT {', '.join(COLUMNS)} FROM messages WHERE topic = ? AND title = ? AND body = ? "  # nosec B608
                      "AND last_ts >= ? AND decision != 'duplicate' ORDER BY id DESC LIMIT 1",
                      (topic, title, body, since))
        return r[0] if r else None

    def fold(self, msg_id: int, ts: float) -> None:
        self._q("UPDATE messages SET repeats = repeats + 1, last_ts = ? WHERE id = ?", (ts, msg_id))

    def recent(self, limit: int = 200, topic: str = "") -> list[dict]:
        sql = f"SELECT {', '.join(COLUMNS)} FROM messages"  # nosec B608 - fixed column list
        args: list = []
        if topic:
            sql += " WHERE topic = ?"
            args.append(topic)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return self.rows(sql, args)

    def pending(self, max_attempts: int = 20) -> list[dict]:
        """Messages to deliver that some output has not taken yet."""
        return self.rows(f"SELECT {', '.join(COLUMNS)} FROM messages WHERE decision = 'deliver' "  # nosec B608
                         "AND delivered = '' AND attempts < ? ORDER BY id", (max_attempts,))

    def mark(self, msg_id: int, delivered: str, failed: bool) -> None:
        self._q("UPDATE messages SET delivered = ?, attempts = attempts + ? WHERE id = ?",
                (delivered, 1 if failed else 0, msg_id))

    def held_back(self) -> list[dict]:
        """Quiet-hour messages not summarised yet."""
        return self.rows(f"SELECT {', '.join(COLUMNS)} FROM messages WHERE decision = 'quiet' AND digested = 0 "  # nosec B608
                         "ORDER BY id")

    def mark_digested(self, ids: list[int]) -> None:
        if ids:
            marks = ",".join("?" * len(ids))
            self._q(f"UPDATE messages SET digested = 1 WHERE id IN ({marks})", ids)  # nosec B608

    def counts(self, since: float) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT decision, COUNT(*), SUM(repeats) FROM messages WHERE ts >= ? GROUP BY decision",
                                     (since,)).fetchall()
        return {d: {"messages": n, "repeats": int(r or 0)} for d, n, r in rows}

    def get(self, key: str, default: str = "") -> str:
        with self.lock:
            r = self.conn.execute("SELECT v FROM state WHERE k = ?", (key,)).fetchone()
        return r[0] if r else default

    def put(self, key: str, value: str) -> None:
        self._q("INSERT INTO state (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    def trim(self, before: float) -> int:
        return self._q("DELETE FROM messages WHERE last_ts < ?", (before,)).rowcount
