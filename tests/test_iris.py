import json
import os
import stat
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from iris import __main__ as cli
from iris import config
from iris.config import ConfigError, Settings, in_quiet, priority
from iris.core import Hub, clean, message, summary, topic_ok
from iris.store import Store

T0 = 1_790_000_000.0   # a fixed moment; local hours come from the `hours` map below


def hub(settings=None, outputs=None, hour=12):
    s = settings or Settings()
    s.digest_at = s.digest_at if settings else ""
    store = Store(":memory:")
    return Hub(s, store, outputs if outputs is not None else {}, clock=lambda: T0,
               localtime=lambda ts: datetime(2026, 11, 24, hour, 30)), store


def msg(body="disk full", title="Hestia", topic="hestia", prio="default", ts=T0):
    return message(topic, {"Title": title, "Priority": prio}, body.encode(), ts)


# --- config -----------------------------------------------------------------------------

def test_priorities_and_quiet_hours():
    assert [priority(x) for x in ("1", "5", "max", "HIGH", "nonsense", "")] == ["min", "urgent", "urgent", "high", "default", "default"]
    assert in_quiet(23, 23, 7) and in_quiet(3, 23, 7) and not in_quiet(7, 23, 7) and not in_quiet(12, 23, 7)
    assert in_quiet(13, 12, 14) and not in_quiet(14, 12, 14) and not in_quiet(5, None, None)


def test_config_parse_and_errors(tmp_path):
    s = config.parse({"deliver": {"ntfy": "https://ntfy.sh/x", "desktop": False}, "quiet": {"hours": "22-06", "pass": "urgent"},
                      "dedup": {"minutes": 5}, "digest": {"at": "07:30"}, "topics": {"mnemosyne": {"min": "default"}}})
    assert (s.ntfy, s.desktop, s.quiet_start, s.quiet_end, s.quiet_pass) == ("https://ntfy.sh/x", False, 22, 6, "urgent")
    assert s.topic_min == {"mnemosyne": "default"} and s.digest_at == "07:30" and s.dedup_minutes == 5
    for bad in ({"deliver": {"ntfy": "file:///etc/passwd"}}, {"quiet": {"hours": "late"}}, {"quiet": {"hours": "1-30"}},
                {"digest": {"at": "25:00"}}):
        with pytest.raises(ConfigError):
            config.parse(bad)
    f = tmp_path / "c.toml"
    f.write_text("[quiet\n")
    with pytest.raises(ConfigError):
        config.load(f)
    assert config.load(tmp_path / "missing.toml").desktop is True


# --- messages and decisions --------------------------------------------------------------

def test_message_from_ntfy_headers():
    m = message("cerberus", {"x-title": "New\x1b[31m device", "X-Priority": "4", "Tags": "dog,warning",
                             "Click": "javascript:alert(1)"}, b"line one\nline\x07 two" + b"x" * 9000, T0)
    assert m["title"] == "New[31m device" and m["priority"] == "high" and m["tags"] == "dog,warning"
    assert m["click"] == "" and m["body"].startswith("line one\nline two") and len(m["body"]) <= 4096
    assert message("t", {"Click": "https://x.example/#a"}, b"", T0)["click"] == "https://x.example/#a"
    assert message("t", {}, b"", T0)["title"] == "t"
    assert topic_ok("hestia-2_x") and not topic_ok("../etc") and not topic_ok("") and not topic_ok("a" * 65)
    assert clean("a\tb‮", 10) == "a b"


def test_dedup_folds_repeats_within_the_window():
    h, store = hub()
    first, d1 = h.receive(msg(ts=T0))
    again, d2 = h.receive(msg(ts=T0 + 60))
    assert (d1, d2, again) == ("deliver", "duplicate", first)
    other, d3 = h.receive(msg(body="disk ok", ts=T0 + 70))
    assert d3 == "deliver" and other != first
    later, d4 = h.receive(msg(ts=T0 + 31 * 60 + 61))
    assert d4 == "deliver" and later != first
    [m] = [x for x in store.recent() if x["id"] == first]
    assert m["repeats"] == 1


def test_quiet_hours_and_topic_minimum():
    s = Settings(quiet_start=23, quiet_end=7, quiet_pass="high", topic_min={"mnemosyne": "default"})
    h, _ = hub(s, hour=2)
    assert h.receive(msg(prio="default"))[1] == "quiet"
    assert h.receive(msg(body="intruder", prio="high"))[1] == "deliver"
    assert h.receive(msg(body="backup ok", topic="mnemosyne", prio="low"))[1] == "below_min"
    h2, _ = hub(s, hour=12)
    assert h2.receive(msg(prio="default"))[1] == "deliver"


def test_delivery_retry_and_no_double_sends():
    sent, broken = [], {"on": True}

    def flaky(m):
        if broken["on"]:
            raise OSError("offline")
        sent.append(("ntfy", m["id"]))

    h, store = hub(outputs={"ntfy": flaky})
    h.receive(msg())
    assert h.deliver() == 0 and store.pending()[0]["attempts"] == 1
    broken["on"] = False
    assert h.deliver() == 1 and sent == [("ntfy", 1)] and store.pending() == []
    assert h.deliver() == 0, "never sent twice"

    desk = []
    h2, store2 = hub(outputs={"ntfy": lambda m: (_ for _ in ()).throw(OSError()), "desktop": lambda m: desk.append(m["id"])})
    h2.receive(msg())
    assert h2.deliver() == 1 and desk == [1]
    assert store2.recent()[0]["delivered"] == "desktop", "one working output is enough"


def test_morning_digest_once_a_day():
    s = Settings(quiet_start=23, quiet_end=7, digest_at="08:00")
    store = Store(":memory:")
    clock = {"hour": 2}
    h = Hub(s, store, {}, localtime=lambda ts: datetime(2026, 11, 24, clock["hour"], 0))
    for i in range(3):
        h.receive(msg(body=f"backup {i}", topic="mnemosyne", prio="low"))
    h.receive(msg(body="battery", prio="default"))
    assert h.digest(T0) is None, "not before 08:00"
    clock["hour"] = 8
    d = h.digest(T0)
    assert d["title"] == "Iris: 4 message(s) held back overnight" and d["body"].startswith("3 from mnemosyne\n1 from hestia")
    assert h.digest(T0) is None, "once a day"
    assert store.held_back() == []
    assert h.deliver() == 1


def test_summary_and_private_database(tmp_path):
    store = Store(tmp_path / "s" / "iris.db")
    assert stat.S_IMODE(os.stat(tmp_path / "s" / "iris.db").st_mode) == 0o600
    h = Hub(Settings(digest_at=""), store, {})
    h.receive(msg(ts=T0))
    h.receive(msg(ts=T0 + 1))
    s = summary(store, T0 + 10)
    assert s["messages_24h"] == 2 and s["decisions"]["deliver"] == {"messages": 1, "repeats": 1}
    assert s["undelivered"] == 1


# --- the HTTP server -------------------------------------------------------------------

@pytest.fixture
def server():
    from iris.server import serve
    store = Store(":memory:")
    got = []
    h = Hub(Settings(digest_at=""), store, {"test": lambda m: got.append(m["title"])})
    srv = serve(h, "127.0.0.1", 0, token="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", store, got, srv
    srv.shutdown()
    srv.iris_stop.set()
    srv.iris_wake.set()


def call(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_publish_like_ntfy_and_inbox(server):
    base, store, got, _ = server
    st, body, _ = call(f"{base}/hestia", b"BAT0 is 34% worn", {"Title": "Battery", "Priority": "high"}, "POST")
    assert st == 200 and json.loads(body)["decision"] == "deliver"
    assert call(f"{base}/hestia", b"x", {}, "PUT")[0] == 200
    assert call(f"{base}/../etc", b"x", {}, "POST")[0] == 400
    assert call(f"{base}/hestia", b"x", {"Origin": "https://evil.example"}, "POST")[0] == 403
    assert call(f"{base}/hestia", b"x" * 20000, {}, "POST")[0] == 413
    for _ in range(50):
        if got:
            break
        threading.Event().wait(0.05)
    assert "Battery" in got, "delivered right after it arrived"
    st, body, headers = call(f"{base}/api/messages?topic=hestia")
    assert st == 200 and json.loads(body)[-1]["title"] == "Battery"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    st, page, _ = call(base + "/")
    assert st == 200 and b"<title>Iris</title>" in page
    assert call(base + "/static/app.js")[0] == 200
    assert json.loads(call(base + "/api/status")[1])["messages_24h"] == 2


def test_token_on_the_network():
    from iris.server import serve
    h = Hub(Settings(digest_at=""), Store(":memory:"), {})
    srv = serve(h, "127.0.0.1", 0, token="a-long-enough-token")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert call(f"{base}/t", b"x", {}, "POST")[0] == 401
        assert call(f"{base}/t", b"x", {"Authorization": "Bearer a-long-enough-token"}, "POST")[0] == 200
        assert call(f"{base}/api/messages")[0] == 401
        assert call(base + "/")[0] == 401 or call(base + "/")[0] == 404
        assert call(base + "/healthz")[0] == 200
    finally:
        srv.shutdown()
        srv.iris_stop.set()
        srv.iris_wake.set()


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IRIS_DB", str(tmp_path / "i.db"))
    cfg = tmp_path / "c.toml"
    cfg.write_text("[deliver]\ndesktop = false\n")
    assert cli.main(["--config", str(cfg), "send", "test", "hello", "--title", "Hi"]) == 0
    assert "deliver" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "send", "../x", "hello"]) == 2
    assert cli.main(["--config", str(cfg), "inbox"]) == 0
    assert "Hi" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "status", "--save", str(tmp_path / "st.json")]) == 0
    assert json.loads((tmp_path / "st.json").read_text())["messages_24h"] == 1
    assert cli.main(["--config", str(cfg), "demo", "--db", str(tmp_path / "d.db")]) == 0
    cfg.write_text("[quiet]\nhours = 'x'\n")
    assert cli.main(["--config", str(cfg), "inbox"]) == 2
    assert cli.main(["--config", str(tmp_path / "none.toml"), "serve", "--host", "0.0.0.0"]) == 2, "no token: refused"


def test_worker_writes_status_each_round(tmp_path):
    from iris.server import serve
    store = Store(":memory:")
    path = tmp_path / "st" / "status.json"
    h = Hub(Settings(digest_at=""), store, {})
    srv = serve(h, "127.0.0.1", 0, on_round=lambda: cli.save_status(store, path))
    try:
        for _ in range(50):
            if path.exists():
                break
            threading.Event().wait(0.05)
        data = json.loads(path.read_text())
        assert data["messages_24h"] == 0 and data["undelivered"] == 0
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        srv.iris_stop.set()
        srv.iris_wake.set()
        srv.server_close()
