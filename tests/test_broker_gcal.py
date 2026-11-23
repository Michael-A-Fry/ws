"""The broker's `gcal` kind: the Google refresh token stays in the broker, which runs only the fixed OPS table, and every
write type has a switch that is off until a person at a terminal turns it on. Against a fake Google API in-process (the
one HTTPS seam, ws.minters.gcal._https, replaced), no network. The planted client secret, refresh token and access
token never reach a reply, the audit log, stderr or an exception text."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path

import pytest
from helpers.stub_secrets import StubSecrets, planted_value

from ws import broker
from ws import secrets as wsecrets
from ws.minters import gcal

CAL = "c-abc123ws.calendar.test"
EVENT = {"summary": "Dentist", "start": {"dateTime": "2026-11-26T09:00:00+13:00"},
         "end": {"dateTime": "2026-11-26T09:30:00+13:00"}, "description": "from a mail"}  # fmt: skip
RANGE = {"time_min": "2026-11-25T00:00:00+13:00", "time_max": "2026-11-26T00:00:00+13:00"}


class FakeGoogle:
    """Google's token endpoint and Calendar API, as far as ws uses them. Records every request it is sent."""

    def __init__(self, refresh: str, secret: str):
        self.refresh, self.secret = refresh, secret
        self.access = planted_value()
        self.calls: list[tuple[str, str, str, dict, dict | None]] = []  # host, method, path, query, body
        self.events: dict[str, dict] = {}  # on the ws calendar
        self.primary = [{"id": "his1", "summary": "Standup", "status": "confirmed",
                         "start": {"dateTime": "2026-11-25T09:00:00+13:00"}, "end": {"dateTime": "2026-11-25T09:15:00+13:00"}},
                        {"id": "his2", "summary": "Gone", "status": "cancelled"}]  # fmt: skip
        self.expire_once = False
        self.leak = False  # an answer that holds the access token
        self.access_ok = True  # False: every API call is a 401, the fresh token's too

    def __call__(self, host, method, path, headers, body, timeout):
        u = urllib.parse.urlsplit(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if host == gcal.TOKEN_HOST:
            form = dict(urllib.parse.parse_qsl(body.decode()))
            self.calls.append((host, method, u.path, {}, {k: "?" for k in form}))
            if form.get("refresh_token") != self.refresh or form.get("client_secret") != self.secret:
                return 400, b'{"error":"invalid_grant"}'
            return 200, json.dumps({"access_token": self.access, "expires_in": 3600}).encode()
        assert host == gcal.API_HOST
        data = json.loads(body) if body else None
        self.calls.append((host, method, u.path, q, data))
        if not self.access_ok or headers.get("Authorization") != f"Bearer {self.access}":
            return 401, b'{"error":{"message":"bad token"}}'
        if self.expire_once:
            self.expire_once = False
            self.access = planted_value()
            return 401, b"{}"
        ws_base = f"/calendar/v3/calendars/{urllib.parse.quote(CAL, safe='')}/events"
        if u.path == "/calendar/v3/calendars/primary/events" and method == "GET":
            items = self.primary + ([{"id": "x", "summary": self.access}] if self.leak else [])
            return 200, json.dumps({"items": items}).encode()
        if u.path == ws_base and method == "GET":
            return 200, json.dumps({"items": list(self.events.values())}).encode()
        if u.path == ws_base and method == "POST":
            if data["id"] in self.events:
                return 409, b'{"error":{"message":"The requested identifier already exists."}}'
            ev = {**data, "status": "confirmed", "htmlLink": "https://calendar.google.com/x"}
            self.events[data["id"]] = ev
            return 200, json.dumps(ev).encode()
        if u.path.startswith(ws_base + "/"):
            eid = urllib.parse.unquote(u.path[len(ws_base) + 1 :])
            ev = self.events.get(eid)
            if ev is None:
                return 404, b"{}"
            if method == "GET":
                return 200, json.dumps(ev).encode()
            if method == "PATCH":
                ev.update(data)
                return 200, json.dumps(ev).encode()
        if u.path == "/calendar/v3/freeBusy" and method == "POST":
            return 200, json.dumps({"calendars": {"primary": {"busy": [{"start": "2026-11-25T09:00:00+13:00",
                                    "end": "2026-11-25T09:15:00+13:00"}]}, CAL: {"busy": []}}}).encode()  # fmt: skip
        return 404, b"{}"

    def writes(self):
        return [c for c in self.calls if c[0] == gcal.API_HOST and c[1] != "GET"]


class Box:
    def __init__(self, base: Path, monkeypatch, entry_extra: str = "", switches: dict | None = None, terminal=True):
        gcal.forget_tokens()
        self.s = StubSecrets(base)
        self.secret, self.refresh = planted_value(), planted_value()
        self.s.registry.write_text(f'[gcal]\nkind = "gcal"\n{entry_extra}', encoding="utf-8")
        held = gcal.Held("123456-abcdefghijkl.apps.googleusercontent.com", self.secret, self.refresh, CAL)
        self.s.store.write("gcal", gcal.dump_held(held))
        self.pin = "pin-" + os.urandom(4).hex()
        self.s.store.write(gcal.SWITCH_PIN_NAME, self.pin)
        self.fake = FakeGoogle(self.refresh, self.secret)
        monkeypatch.setattr(gcal, "_https", self.fake)
        self.log = base / "logs" / "broker.jsonl"
        self.sock = base / "run" / "b.sock"
        self.broker = broker.Broker(self.sock, self.s.registry, self.s.store, log_path=self.log, records=self.s.records)
        self.broker.gcal_switch_file = base / "gcal-switches.json"
        self.broker.peer_has_terminal = lambda pid: terminal
        if switches:
            broker.write_gcal_switches(self.broker.gcal_switch_file, switches)
        self.broker.start()

    def call(self, cmd: str, **args) -> dict:
        return broker.gcal_call("gcal", cmd, args, socket=self.sock)

    def refused(self, cmd: str, **args) -> broker.BrokerRefused:
        with pytest.raises(broker.BrokerRefused) as e:
            self.call(cmd, **args)
        return e.value

    def lines(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def needles(self) -> list[str]:
        return [self.secret, self.refresh, self.fake.access, self.pin]

    def flip(self, write: str, state: str, pin: str | None = None) -> dict:
        return broker.gcal_switch(write, state, self.sock, pin=self.pin if pin is None and state == "on" else pin)

    def close(self):
        self.broker.stop()


@pytest.fixture
def box(tmp_path, monkeypatch):
    made = []

    def make(**kw):
        b = Box(tmp_path / f"b{len(made)}", monkeypatch, **kw)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


ON = {"insert": True, "patch": True}


# ---------------------------------------------------------------- the op table


@pytest.mark.parametrize("cmd", ["delete", "move", "quickAdd", "import", "instances", "calendars.delete", "clear", ""])
def test_ops_outside_the_table_are_unreachable(box, cmd):
    b = box(switches=ON)
    assert b.refused(cmd).code == "op_refused"
    assert b.fake.calls == []  # refused before any credential is read or Google is dialled


def test_no_request_ever_deletes_moves_imports_or_quick_adds(box):
    b = box(switches=ON)
    ins = b.call("insert", ws_id="mail:1", event=EVENT)
    b.call("insert", ws_id="mail:1", event=EVENT)
    b.call(
        "patch", event_id=ins["event"]["id"], event={"summary": "[ws mistake] Dentist", "transparency": "transparent"}
    )
    b.call("list", **RANGE)
    b.call("list", calendar="ws", **RANGE)
    b.call("freebusy", **RANGE)
    b.call("get", calendar="ws", event_id=ins["event"]["id"])
    methods = {c[1] for c in b.fake.calls}
    assert methods <= {"GET", "POST", "PATCH"}
    for c in b.fake.calls:
        assert not any(w in c[2] for w in ("/move", "/quickAdd", "/import", "/clear", "/delete"))
        assert c[2] in ("/token", "/calendar/v3/freeBusy") or c[2].startswith("/calendar/v3/calendars/")


def test_reads_list_his_events_and_drop_cancelled(box):
    b = box()  # reads need no switch
    out = b.call("list", **RANGE)
    assert [e["id"] for e in out["events"]] == ["his1"]
    assert out["calendar"] == "primary"
    fb = b.call("freebusy", **RANGE)
    assert fb["busy"] == [
        {"start": "2026-11-25T09:00:00+13:00", "end": "2026-11-25T09:15:00+13:00", "calendar": "primary"}
    ]


def test_bad_ranges_are_refused_before_google(box):
    b = box()
    assert b.refused("list", time_min="yesterday", time_max=RANGE["time_max"]).code == "bad_request"
    assert b.refused("list", time_min=RANGE["time_max"], time_max=RANGE["time_min"]).code == "bad_request"
    assert b.refused("list", calendar="someone-else", **RANGE).code == "bad_request"
    assert b.fake.calls == []


def test_a_read_only_entry_refuses_writes(box):
    b = box(entry_extra='allow = ["read"]\n', switches=ON)
    assert b.refused("insert", ws_id="mail:1", event=EVENT).code == "op_refused"
    assert b.fake.writes() == []


# ---------------------------------------------------------------- write fields


@pytest.mark.parametrize("field", ["attendees", "conferenceData", "status", "id", "recurrence", "extendedProperties",
                                   "guestsCanInviteOthers", "organizer"])  # fmt: skip
def test_fields_outside_the_whitelist_are_refused(box, field):
    b = box(switches=ON)
    ev = {**EVENT, field: [{"email": "guest"}] if field == "attendees" else "x"}
    assert b.refused("insert", ws_id="mail:2", event=ev).code == "field_refused"
    assert b.fake.writes() == []


def test_every_write_sends_updates_none_and_the_marker(box):
    b = box(switches=ON)
    ins = b.call("insert", ws_id="mail:3", event=EVENT)
    b.call("patch", event_id=ins["event"]["id"], event={"location": "Tauranga"})
    writes = b.fake.writes()
    assert len(writes) == 2
    for _h, _m, _p, q, _b in writes:
        assert q == {"sendUpdates": "none"}
    body = writes[0][4]
    assert body["extendedProperties"] == {"private": {"ws_id": "mail:3"}}
    assert body["id"] == gcal.event_id("mail:3")
    assert set(body) <= gcal.EVENT_FIELDS | {"id", "extendedProperties"}
    assert ins["event"]["ws_id"] == "mail:3"


def test_event_ids_are_base32hex_and_stable():
    a = gcal.event_id("mail:abc")
    assert a == gcal.event_id("mail:abc") != gcal.event_id("mail:abz")
    assert gcal.EVENT_ID_RE.match(a)


def test_a_retried_insert_makes_no_second_event(box):
    b = box(switches=ON)
    first = b.call("insert", ws_id="mail:4", event=EVENT)
    again = b.call("insert", ws_id="mail:4", event=EVENT)
    assert first["existing"] is False and again["existing"] is True
    assert first["event"]["id"] == again["event"]["id"]
    assert len(b.fake.events) == 1


def test_a_409_on_someone_elses_event_is_a_conflict(box):
    b = box(switches=ON)
    eid = gcal.event_id("mail:5")
    b.fake.events[eid] = {"id": eid, "summary": "not ws's", "extendedProperties": {"private": {"ws_id": "other"}}}
    assert b.refused("insert", ws_id="mail:5", event=EVENT).code == "conflict"


def test_a_patch_on_an_event_without_the_marker_is_refused(box):
    b = box(switches=ON)
    eid = gcal.event_id("hand-made")
    b.fake.events[eid] = {"id": eid, "summary": "his own, on the ws calendar"}
    assert b.refused("patch", event_id=eid, event={"summary": "changed"}).code == "not_ws_event"
    assert not [c for c in b.fake.calls if c[1] == "PATCH"]
    assert b.fake.events[eid]["summary"] == "his own, on the ws calendar"


def test_a_patch_cannot_cancel(box):
    b = box(switches=ON)
    ins = b.call("insert", ws_id="mail:6", event=EVENT)
    assert b.refused("patch", event_id=ins["event"]["id"], event={"status": "cancelled"}).code == "field_refused"


def test_a_patch_names_a_ws_style_id_only(box):
    b = box(switches=ON)
    assert b.refused("patch", event_id="his1", event={"summary": "x"}).code == "bad_request"


# ---------------------------------------------------------------- switches


def test_writes_are_off_by_default(box):
    b = box()
    assert b.refused("insert", ws_id="mail:7", event=EVENT).code == "switch_off"
    assert b.refused("patch", event_id=gcal.event_id("mail:7"), event={"summary": "x"}).code == "switch_off"
    assert b.fake.writes() == [] and b.fake.calls == []
    assert broker.gcal_switch(None, None, b.sock)["switches"] == {"insert": False, "patch": False}


def test_one_switch_does_not_open_the_other(box):
    b = box(switches={"insert": True})
    ins = b.call("insert", ws_id="mail:8", event=EVENT)
    assert b.refused("patch", event_id=ins["event"]["id"], event={"summary": "x"}).code == "switch_off"


def test_a_flip_writes_an_audit_line_and_moves_the_switch(box):
    b = box()
    out = b.flip("insert", "on")
    assert out["switches"]["insert"] is True and out["changed"] is True
    assert b.call("insert", ws_id="mail:9", event=EVENT)["existing"] is False
    b.flip("insert", "off")
    assert b.refused("insert", ws_id="mail:10", event=EVENT).code == "switch_off"
    flips = [x for x in b.lines() if x["op"] == "gcal_switch"]
    assert [(x["purpose"], x["status"]) for x in flips] == [("insert:on", "on"), ("insert:off", "off")]
    assert all(x["uid"] == os.geteuid() for x in flips)


def test_on_needs_a_terminal_off_does_not(box):
    b = box(terminal=False)
    with pytest.raises(broker.BrokerRefused) as e:
        b.flip("insert", "on")
    assert e.value.code == "caller_refused"
    assert b.flip("insert", "off")["switches"]["insert"] is False
    refused = [x for x in b.lines() if x["op"] == "gcal_switch" and x["status"] == "caller_refused"]
    assert len(refused) == 1  # a refused flip is a line too
    assert b.refused("insert", ws_id="mail:11", event=EVENT).code == "switch_off"


def test_a_switch_file_another_user_could_write_counts_as_off(tmp_path):
    f = tmp_path / "s.json"
    broker.write_gcal_switches(f, {"insert": True})
    assert broker.read_gcal_switches(f, os.geteuid())["insert"] is True
    f.chmod(0o666)
    assert broker.read_gcal_switches(f, os.geteuid()) == {"insert": False, "patch": False}
    f.write_text("not json")
    f.chmod(0o600)
    assert broker.read_gcal_switches(f, os.geteuid())["insert"] is False
    assert broker.read_gcal_switches(None, os.geteuid())["insert"] is False


def test_the_terminal_check_reads_the_controlling_terminal():
    import subprocess
    import sys

    sleep = "import time; time.sleep(30)"
    take = "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); " + sleep  # the pty becomes its terminal
    loose = subprocess.Popen([sys.executable, "-c", sleep], start_new_session=True, stdin=subprocess.DEVNULL)
    main, sub = os.openpty()
    held = subprocess.Popen([sys.executable, "-c", take], start_new_session=True, stdin=sub)
    try:
        deadline = time.monotonic() + 10
        while not broker.peer_has_terminal(held.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert broker.peer_has_terminal(held.pid) is True  # a session can make itself one: the PIN is the lock
        assert broker.peer_has_terminal(loose.pid) is False  # a new session with no terminal: an agent's tool shell
        assert broker.peer_has_terminal(2**31 - 1) is False
    finally:
        for c in (loose, held):
            c.kill()
            c.wait()
        os.close(main)
        os.close(sub)


def test_a_faked_terminal_without_the_pin_cannot_turn_a_write_on(box):
    b = box()  # the terminal check passes here, as it would for a session that made itself a pty
    for pin in (None, "", "wrong-pin-123"):
        with pytest.raises(broker.BrokerRefused) as e:
            broker.gcal_switch("insert", "on", b.sock, pin=pin)
        assert e.value.code == "caller_refused"
    with pytest.raises(broker.BrokerRefused) as e:  # three misses lock it, the right PIN included
        b.flip("insert", "on")
    assert "restart" in str(e.value)
    assert broker.gcal_switch(None, None, b.sock)["switches"]["insert"] is False
    refused = [x for x in b.lines() if x["op"] == "gcal_switch" and x["status"] == "caller_refused"]
    assert len(refused) == 4 and b.pin not in b.log.read_text()


def test_no_pin_stored_means_no_switch_on(box):
    b = box()
    (b.s.store.path / gcal.SWITCH_PIN_NAME).unlink()
    with pytest.raises(broker.BrokerRefused) as e:
        b.flip("insert", "on", pin="anything-at-all")
    assert e.value.code == "unset"


def test_another_uid_cannot_turn_a_write_on(box):
    b = box()
    b.broker.own_uid = os.geteuid() + 1  # the broker runs as another user (the wall); this caller is not it
    with pytest.raises(broker.BrokerRefused) as e:
        b.flip("insert", "on")
    assert e.value.code == "caller_refused"


def test_two_401s_are_refused_without_a_token(box):
    b = box()
    b.fake.access_ok = False
    e = b.refused("list", **RANGE)
    assert e.code == "upstream_refused"
    assert not any(n in str(e) for n in b.needles())


def test_a_409_on_an_event_he_deleted_is_not_a_success(box):
    b = box(switches=ON)
    eid = gcal.event_id("mail:15")
    b.fake.events[eid] = {"id": eid, "status": "cancelled", "extendedProperties": {"private": {"ws_id": "mail:15"}}}
    assert b.refused("insert", ws_id="mail:15", event=EVENT).code == "deleted"


def test_get_refuses_dot_ids(box):
    b = box()
    for eid in (".", "..", "../x"):
        assert b.refused("get", calendar="primary", event_id=eid).code == "bad_request"


# ---------------------------------------------------------------- credentials


def test_tokens_never_reach_a_reply_the_log_or_stderr(box, capfd):
    b = box(switches=ON)
    replies = [b.call("list", **RANGE), b.call("freebusy", **RANGE), b.call("insert", ws_id="mail:12", event=EVENT)]
    b.fake.expire_once = True  # a 401 mid-call: the broker refreshes and retries once
    replies.append(b.call("list", calendar="ws", **RANGE))
    errs = [b.refused("insert", ws_id="mail:13", event={**EVENT, "attendees": []}), b.refused("nope")]
    b.fake.leak = True
    errs.append(b.refused("list", **RANGE))
    assert errs[-1].code == "upstream_refused"
    text = json.dumps(replies) + " ".join(str(e) for e in errs) + b.log.read_text()
    cap = capfd.readouterr()
    text += cap.out + cap.err
    for n in b.needles():
        assert n not in text
    assert sum(1 for c in b.fake.calls if c[2] == "/token") == 2


def test_a_refused_refresh_token_says_connect_again(box):
    b = box()
    b.fake.refresh = "something-else"
    e = b.refused("list", **RANGE)
    assert e.code == "login_refused" and "connect" in str(e)
    assert b.refresh not in str(e)


def test_a_stored_value_that_is_not_ours_is_refused(box):
    b = box()
    b.s.store.write("gcal", "just-a-password")
    assert b.refused("list", **RANGE).code == "store_unreadable"


def test_the_write_audit_line_names_the_event(box):
    b = box(switches=ON)
    ins = b.call("insert", ws_id="mail:14", event=EVENT)
    b.call("insert", ws_id="mail:14", event=EVENT)
    oks = [x["purpose"] for x in b.lines() if x["op"] == "gcal" and x["status"] == "ok"]
    assert oks == [f"insert:{ins['event']['id']}", f"insert:{ins['event']['id']}:existing"]


# ---------------------------------------------------------------- registry and consent


def test_registry_keys_are_checked(tmp_path):
    reg = tmp_path / "secrets.toml"
    reg.write_text('[gcal]\nkind = "gcal"\nallow = ["delete"]\n')
    reg.chmod(0o600)
    with pytest.raises(wsecrets.SecretsError):
        wsecrets.load_registry(reg)
    reg.write_text('[gcal]\nkind = "gcal"\nhosts = ["evil.example.com"]\n')
    with pytest.raises(wsecrets.SecretsError):
        wsecrets.load_registry(reg)
    reg.write_text('[gcal]\nkind = "gcal"\n')
    e = wsecrets.load_registry(reg)["gcal"]
    assert e.options == gcal.Options()


def test_consent_asks_only_the_three_scopes():
    url = gcal.auth_url("1-x.apps.googleusercontent.com", "http://127.0.0.1:8765/", "st", "ch")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["scope"][0].split() == list(gcal.SCOPES)
    assert q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    assert not any(s.endswith(("/auth/calendar", "calendar.events")) for s in gcal.SCOPES)


def test_the_pasted_address_must_carry_this_attempts_state():
    ok = "http://127.0.0.1:8765/?state=st&code=4/abc-def_ghi&scope=" + "+".join(gcal.SCOPES)
    assert gcal.code_from(ok, "st") == "4/abc-def_ghi"
    with pytest.raises(gcal.GcalError):
        gcal.code_from(ok, "other")
    with pytest.raises(gcal.GcalError):
        gcal.code_from("http://127.0.0.1:8765/?state=st&error=access_denied", "st")
    with pytest.raises(gcal.GcalError):  # a box left unticked
        gcal.code_from("http://127.0.0.1:8765/?state=st&code=4/abcdefghij&scope=" + gcal.SCOPES[0], "st")


def test_ensure_calendar_keeps_the_old_one_or_makes_ws(monkeypatch):
    seen = []

    def fake(host, method, path, headers, body, timeout):
        seen.append((method, path, json.loads(body) if body and host == gcal.API_HOST else None))
        if host == gcal.TOKEN_HOST:
            return 200, b'{"access_token":"t0kenvalue","expires_in":3600}'
        if method == "GET":
            return (200, json.dumps({"id": "old.group"}).encode()) if "old.group" in path else (404, b"{}")
        return 200, b'{"id":"new.group.calendar.test"}'

    monkeypatch.setattr(gcal, "_https", fake)
    gcal.forget_tokens()
    assert (
        gcal.ensure_calendar("1-x.apps.googleusercontent.com", "s" * 12, "r" * 12, "Pacific/Auckland", "old.group")
        == "old.group"
    )
    assert (
        gcal.ensure_calendar("1-x.apps.googleusercontent.com", "s" * 12, "r" * 12, "Pacific/Auckland", "gone.group")
        == "new.group.calendar.test"
    )
    made = [s for s in seen if s[0] == "POST" and s[1] == "/calendar/v3/calendars"]
    assert made[0][2]["summary"] == "ws"
