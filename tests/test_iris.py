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


def test_rules_first_match_wins():
    from iris.config import Rule
    s = config.parse({"quiet": {"hours": "23-07"}, "topics": {"mnemosyne": {"min": "default"}}, "rules": [
        {"topic": "mnemosyne", "match": "backup FAILED", "action": "always", "priority": "urgent"},
        {"topic": "mnemosyne", "match": "Backup finished", "action": "store"},
        {"match": "decoy", "priority": "urgent"},
        {"topic": "hestia", "action": "hold"}]})
    assert s.rules[0] == Rule(match="backup FAILED", topic="mnemosyne", action="always", priority="urgent")
    h, store = hub(s, hour=3)   # in quiet hours
    mid, d = h.receive(msg("Backup failed: disk Z missing", title="Mnemosyne", topic="mnemosyne", prio="low"))
    assert d == "deliver" and store.recent()[0]["priority"] == "urgent", "rescued from both the minimum and the night"
    assert h.receive(msg("Backup finished: 12 files", title="Mnemosyne", topic="mnemosyne", prio="high"))[1] == "below_min"
    assert h.receive(msg("41 attempts on the decoy", title="Cerberus", topic="cerberus", prio="default"))[1] == "deliver", \
        "priority raised to urgent, which passes quiet hours"
    h2, _ = hub(s, hour=12)
    assert h2.receive(msg("battery", topic="hestia", prio="urgent"))[1] == "quiet", "hold even by day"
    assert h2.receive(msg("other", topic="themis", prio="low"))[1] == "deliver", "no rule: the usual way"
    for bad in ([{"match": "x", "action": "mute"}], [{"match": "x"}], ["x"], [{"match": "x", "priority": "loud"}]):
        with pytest.raises(ConfigError):
            config.parse({"rules": bad})


# --- tasks ------------------------------------------------------------------------------

from iris.tasks import REOPEN_AFTER, TaskError, validate  # noqa: E402


def task(id="firewall", title="Turn the firewall on", prio="default", **kw):
    return {"id": id, "title": title, "priority": prio, **kw}


def test_validate_tasks():
    out = validate([task(action="line one\nline two\x1b[31m", command="sudo ufw enable\n; rm", link="javascript:x")])
    assert out[0]["action"] == "line one\nline two[31m" and "\n" not in out[0]["command"] and out[0]["link"] == ""
    for bad in ("x", [task(id="Bad Id")], [task(title="")], [task(prio="panic")], [task(), task()], [1] ,
                [task(id=f"t{i}") for i in range(101)]):
        with pytest.raises(TaskError):
            validate(bad)


def test_sync_opens_announces_urgent_and_resolves_gone_ones():
    h, store = hub()
    r = h.sync_tasks("olympos", validate([task(), task("backup", "Backup failed", "high", command="journalctl")]), T0)
    assert (r["new"], r["gone"]) == (2, 0)
    sent = store.recent(10, "tasks")
    assert [m["title"] for m in sent] == ["To do: Backup failed"], "only the urgent one is announced at once"
    assert "$ journalctl" in sent[0]["body"]
    # the same list again: nothing new, nothing announced twice
    r = h.sync_tasks("olympos", validate([task(), task("backup", "Backup failed", "high")]), T0 + 60)
    assert r["kept"] == 2 and len(store.recent(10, "tasks")) == 1
    # the backup works again: its task is gone by itself; another source is untouched
    h.sync_tasks("chronos", validate([task("streak", "Commit today")]), T0 + 90)
    r = h.sync_tasks("olympos", validate([task()]), T0 + 120)
    assert r["gone"] == 1
    assert {t["key"] for t in h.tasks.list(T0 + 120)} == {"olympos:firewall", "chronos:streak"}
    assert h.tasks.counts()["gone"] == 1


def test_done_snooze_and_reopen_when_still_reported():
    h, _ = hub()
    h.sync_tasks("olympos", validate([task()]), T0)
    h.tasks.act("olympos:firewall", "done", T0 + 10)
    h.sync_tasks("olympos", validate([task()]), T0 + 60)
    assert h.tasks.get("olympos:firewall")["state"] == "done", "the source needs a moment to notice"
    h.sync_tasks("olympos", validate([task()]), T0 + 10 + REOPEN_AFTER)
    t = h.tasks.get("olympos:firewall")
    assert t["state"] == "open" and t["reopened"] == 1
    h.tasks.act("olympos:firewall", "snooze", T0 + REOPEN_AFTER + 20, hours=1)
    assert h.tasks.list(T0 + REOPEN_AFTER + 30) == []
    assert len(h.tasks.list(T0 + REOPEN_AFTER + 20 + 3601)) == 1, "the snooze ran out"
    with pytest.raises(TaskError):
        h.tasks.act("olympos:firewall", "snooze", T0, hours=0)
    with pytest.raises(KeyError):
        h.tasks.act("olympos:nope", "done", T0)


def test_daily_plan_lists_open_tasks_in_turkish():
    s = Settings(digest_at="", plan_at="08:30", task_lang="tr")
    store = Store(":memory:")
    clock = {"h": 8, "m": 0}
    h = Hub(s, store, {}, localtime=lambda ts: datetime(2026, 11, 24, clock["h"], clock["m"]))
    assert h.plan(T0) is None
    h.sync_tasks("olympos", validate([task(command="sudo ufw enable"), task("x", "Disk encryption", "low")]), T0)
    assert h.plan(T0) is None, "not yet 08:30"
    clock["m"] = 31
    m = h.plan(T0)
    assert m["title"] == "Bugünün işleri: 2" and m["decision"] == "deliver"
    assert m["body"].splitlines()[:2] == ["1. Turn the firewall on", "   $ sudo ufw enable"]
    assert "· Disk encryption" in m["body"]
    assert h.plan(T0 + 60) is None, "once a day"


def test_task_api(server):
    base, store, got, srv = server
    js = {"Content-Type": "application/json"}
    body = json.dumps({"tasks": [task(), task("backup", "Backup failed", "urgent")]}).encode()
    st, out, _ = call(f"{base}/api/tasks/olympos", body, js, "PUT")
    assert st == 200 and json.loads(out)["new"] == 2
    assert call(f"{base}/api/tasks/olympos", body, {**js, "Origin": "http://127.0.0.1"}, "PUT")[0] == 403
    assert call(f"{base}/api/tasks/olympos", body, {}, "PUT")[0] == 400, "JSON only"
    assert call(f"{base}/api/tasks/../x", body, js, "PUT")[0] in (400, 404)
    assert call(f"{base}/api/tasks/olympos", b'{"tasks": "no"}', js, "PUT")[0] == 400
    tasks = json.loads(call(f"{base}/api/tasks")[1])
    assert [t["key"] for t in tasks] == ["olympos:backup", "olympos:firewall"], "urgent first"
    host = base.removeprefix("http://")
    page = {**js, "Origin": base}
    assert call(f"{base}/api/task/olympos%3Afirewall/done", b"{}", {**js, "Origin": "http://evil.example"}, "POST")[0] == 403
    assert call(f"{base}/api/task/olympos%3Afirewall/done", b"{}", {**page, "Host": "evil.example:80"}, "POST")[0] == 403
    st, out, _ = call(f"{base}/api/task/olympos%3Afirewall/done", b"{}", page, "POST")
    assert st == 200 and json.loads(out)["task"]["state"] == "done", host
    assert call(f"{base}/api/task/olympos%3Afirewall/snooze", b'{"hours": "x"}', page, "POST")[0] == 400
    assert call(f"{base}/api/task/olympos%3Anope/done", b"{}", page, "POST")[0] == 404
    assert call(f"{base}/api/task/olympos%3Afirewall/explode", b"{}", page, "POST")[0] == 404
    assert call(f"{base}/api/tasks?state=weird")[0] == 400
    assert json.loads(call(f"{base}/api/status")[1])["tasks"]["open_urgent"] == 1
    for _ in range(50):
        if any(t.startswith("To do: Backup failed") for t in got):
            break
        threading.Event().wait(0.05)
    assert "To do: Backup failed" in got


def test_task_api_needs_the_token_on_the_network():
    from iris.server import serve
    h = Hub(Settings(digest_at=""), Store(":memory:"), {})
    srv = serve(h, "127.0.0.1", 0, token="a-long-enough-token")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    js = {"Content-Type": "application/json"}
    body = json.dumps({"tasks": [task()]}).encode()
    try:
        assert call(f"{base}/api/tasks/olympos", body, js, "PUT")[0] == 401
        assert call(f"{base}/api/tasks")[0] == 401
        auth = {"Authorization": "Bearer a-long-enough-token"}
        assert call(f"{base}/api/tasks/olympos", body, {**js, **auth}, "PUT")[0] == 200
        st, out, _ = call(f"{base}/api/tasks", headers=auth)
        assert st == 200 and len(json.loads(out)) == 1, "what a phone app will read"
        assert call(f"{base}/api/task/olympos%3Afirewall/done", b"{}", {**js, **auth}, "POST")[0] == 200
    finally:
        srv.shutdown()
        srv.iris_stop.set()
        srv.iris_wake.set()


def test_task_config():
    s = config.parse({"tasks": {"lang": "tr", "plan_at": "09:15"}})
    assert (s.task_lang, s.plan_at) == ("tr", "09:15")
    assert config.parse({"tasks": {"plan_at": ""}}).plan_at == ""
    for bad in ({"tasks": {"lang": "de"}}, {"tasks": {"plan_at": "9"}}, {"tasks": "x"}):
        with pytest.raises(ConfigError):
            config.parse(bad)
