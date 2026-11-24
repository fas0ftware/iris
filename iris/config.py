"""Settings from ~/.config/iris/config.toml (every key optional).

    [deliver]
    ntfy = "https://ntfy.sh/a-long-random-topic"   # forward here ("" = no phone)
    desktop = true                                  # notify-send on this machine

    [quiet]
    hours = "23-07"            # during these hours only messages at or above `pass` go out
    pass = "high"

    [dedup]
    minutes = 30               # the same title and text within this window is counted, not repeated

    [digest]
    at = "08:00"               # one summary of everything held back; "" = never

    [topics.hestia]
    min = "default"            # below this a message is stored but never delivered
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PRIORITIES = ("min", "low", "default", "high", "urgent")   # ntfy's names for 1..5


class ConfigError(ValueError):
    pass


def priority(value, default: str = "default") -> str:
    """ntfy accepts 1-5 or the names (and 'max' for 5)."""
    v = str(value or "").strip().lower()
    if not v:
        return default
    if v.isdigit() and 1 <= int(v) <= 5:
        return PRIORITIES[int(v) - 1]
    if v == "max":
        return "urgent"
    return v if v in PRIORITIES else default


def rank(p: str) -> int:
    return PRIORITIES.index(p)


def valid_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme in ("http", "https") and bool(u.netloc) and len(url) <= 300 and " " not in url


@dataclass
class Settings:
    ntfy: str = ""
    desktop: bool = True
    quiet_start: int | None = None
    quiet_end: int | None = None
    quiet_pass: str = "high"
    dedup_minutes: float = 30
    digest_at: str = "08:00"
    topic_min: dict[str, str] = field(default_factory=dict)
    db: Path = Path()


def default_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "iris" / "config.toml"


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "iris"


def parse(raw: dict) -> Settings:
    s = Settings(db=state_dir() / "iris.db")
    d = raw.get("deliver", {})
    s.ntfy = str(d.get("ntfy", "")).strip()
    if s.ntfy and not valid_url(s.ntfy):
        raise ConfigError("deliver.ntfy must be an http(s) URL")
    s.desktop = bool(d.get("desktop", True))
    q = raw.get("quiet", {})
    hours = str(q.get("hours", "")).strip()
    if hours:
        try:
            a, b = (int(x) for x in hours.split("-"))
        except ValueError:
            raise ConfigError('quiet.hours looks like "23-07"') from None
        if not (0 <= a <= 23 and 0 <= b <= 23):
            raise ConfigError("quiet.hours must be between 0 and 23")
        s.quiet_start, s.quiet_end = a, b
    s.quiet_pass = priority(q.get("pass"), "high")
    s.dedup_minutes = float(raw.get("dedup", {}).get("minutes", 30))
    at = str(raw.get("digest", {}).get("at", "08:00")).strip()
    if at:
        try:
            h, m = (int(x) for x in at.split(":"))
            assert 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, AssertionError):
            raise ConfigError('digest.at looks like "08:00"') from None
    s.digest_at = at
    for topic, t in (raw.get("topics") or {}).items():
        if isinstance(t, dict) and "min" in t:
            s.topic_min[str(topic)] = priority(t["min"])
    if raw.get("db"):
        s.db = Path(str(raw["db"])).expanduser()
    return s


def load(path: Path | None = None) -> Settings:
    path = path or default_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raw = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse(raw)


def in_quiet(hour: int, start: int | None, end: int | None) -> bool:
    if start is None or end is None or start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end
