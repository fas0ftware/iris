"""Fictional messages (documentation addresses) for screenshots."""

from __future__ import annotations

import time

from .store import Store


def fill(store: Store, now: float | None = None) -> None:
    now = time.time() if now is None else now
    rows = [
        (-9 * 3600, "cerberus", "Cerberus [HIGH] new device", "192.0.2.77 joined: Espressif Inc. (00:00:5e:00:53:77), 23/telnet, 80/http", "high", "deliver", "ntfy,desktop", 0),
        (-7 * 3600, "hestia", "Hestia: Battery on laptop", "BAT0 is 34% worn: it holds 66% of what it did when new; plan a replacement", "default", "quiet", "", 0),
        (-6.5 * 3600, "mnemosyne", "Backup finished", "4781 files, 190.5 KiB added", "low", "quiet", "", 11),
        (-5 * 3600, "themis", "Themis [LOW] auth failure", "Wrong password for alex at the lock screen", "low", "quiet", "", 2),
        (-1 * 3600, "iris", "Iris: 17 message(s) held back overnight", "14 from mnemosyne\n3 from themis\n1 from hestia", "low", "deliver", "ntfy,desktop", 0),
        (-50 * 60, "themis", "Themis [HIGH] usb keyboard", "A new USB device that types like a keyboard was plugged in: unnamed device (1a86:e026). If it is not a keyboard you just connected, unplug it (BadUSB).", "high", "deliver", "ntfy,desktop", 0),
        (-20 * 60, "cerberus", "Cerberus [CRITICAL] maze touch", "192.0.2.77 (a device on your network) is trying passwords on the decoy: 41 attempt(s), 2 decoy(s) found", "urgent", "deliver", "ntfy,desktop", 3),
        (-5 * 60, "hestia", "Hestia: Services recovered on laptop", "no failed services (was bad)", "low", "deliver", "ntfy,desktop", 0),
    ]
    for off, topic, title, body, prio, decision, delivered, repeats in rows:
        mid = store.add({"ts": now + off, "topic": topic, "title": title, "body": body, "priority": prio,
                         "tags": "", "click": "", "decision": decision})
        store.mark(mid, delivered, False)
        for _ in range(repeats):
            store.fold(mid, now + off + 60)
        if decision == "quiet":
            store.mark_digested([mid])   # the demo already holds this morning's digest
