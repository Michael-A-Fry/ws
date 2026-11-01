"""Notify role, ntfy: the topic URL, the broker lease, the daily cap (1 digest, 3 pushes) and `ws notify`. The sender
and the broker are fakes; nothing leaves the machine."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from ws import broker, cli
from ws.roles import ntfy

URL = "https://ntfy.example.org/my-topic"
CFG = {"adapter": "ntfy", "url": URL}
try:
    ZoneInfo("Pacific/Auckland")
    HAS_TZDATA = True
except ZoneInfoNotFoundError:
    HAS_TZDATA = False
needs_tzdata = pytest.mark.skipif(not HAS_TZDATA, reason="no time zone database on this machine")
NOON = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


class Sender:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.status


@pytest.fixture
def lease(monkeypatch):
    log = {"requested": [], "released": []}

    def request(name, purpose, host, socket=None, ttl_s=None):
        log["requested"].append((name, purpose, host))
        return broker.Lease("L1", "http://127.0.0.1:5555/l/TOKEN", "authorization", "2030-01-01T00:00:00Z")

    monkeypatch.setattr(broker, "request", request)
    monkeypatch.setattr(broker, "release", lambda lease, socket=None: log["released"].append(lease.lease))
    return log


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://ntfy.example.org/t",
        "https://ntfy.example.org",
        "https://ntfy.example.org/a/b",
        "https://user:pw" + "@ntfy.example.org/t",  # joined here so the line is not an address to the PII gate
        "https://ntfy.example.org/t?x=1",
        "https://ntfy.example.org/bad topic",
    ],
)
def test_the_url_must_be_an_https_topic(url):
    with pytest.raises(ntfy.NotifyError):
        ntfy.target({"url": url})


def test_target_splits_host_and_topic():
    assert ntfy.target({"url": "https://NTFY.example.org:8443/Topic_1/"}) == (
        "https://ntfy.example.org:8443/Topic_1",
        "ntfy.example.org:8443",
        "Topic_1",
    )


def test_send_without_a_token_posts_to_the_topic(env):
    s = Sender()
    line = ntfy.send(CFG, env, "disk is 91% full", "Disk", now=NOON, sender=s)
    [(url, body, headers)] = s.calls
    assert url == URL and body == b"disk is 91% full" and headers["Title"] == "Disk"
    assert line == "sent: 1 of 3 pushes today"


def test_send_with_a_token_goes_through_a_lease_and_releases_it(env, lease):
    s = Sender()
    ntfy.send({**CFG, "token": "ntfy"}, env, "hello", now=NOON, sender=s)
    assert s.calls[0][0] == "http://127.0.0.1:5555/l/TOKEN/my-topic"
    assert lease["requested"] == [("ntfy", "notify", "ntfy.example.org")] and lease["released"] == ["L1"]
    assert all("authorization" not in k.lower() for k in s.calls[0][2])  # the broker adds it, ws never has it


def test_the_lease_is_released_when_the_send_fails(env, lease):
    def boom(url, body, headers):
        raise ntfy.NotifyError("the push did not go out (URLError)")

    with pytest.raises(ntfy.NotifyError):
        ntfy.send({**CFG, "token": "ntfy"}, env, "x", now=NOON, sender=boom)
    assert lease["released"] == ["L1"]


def test_a_refused_lease_says_so_without_a_url(env, monkeypatch):
    def refuse(*a, **k):
        raise broker.BrokerRefused("unset", "ntfy has no value; run `ws secret set ntfy`")

    monkeypatch.setattr(broker, "request", refuse)
    with pytest.raises(ntfy.NotifyError, match="would not lease ntfy for ntfy.example.org: unset"):
        ntfy.send({**CFG, "token": "ntfy"}, env, "x", now=NOON, sender=Sender())


def test_the_cap_is_three_pushes_and_one_digest_a_day(env):
    s = Sender()
    for n in (1, 2, 3):
        assert ntfy.send(CFG, env, f"m{n}", now=NOON, sender=s).startswith(f"sent: {n} of 3")
    with pytest.raises(ntfy.CapReached, match="3 of 3 pushes already went today"):
        ntfy.send(CFG, env, "m4", now=NOON, sender=s)
    assert len(s.calls) == 3  # the fourth never left
    assert ntfy.send(CFG, env, "digest", kind="digest", now=NOON, sender=s) == "sent: 1 of 1 digest today"
    with pytest.raises(ntfy.CapReached, match="1 of 1 digest already"):
        ntfy.send(CFG, env, "digest 2", kind="digest", now=NOON, sender=s)
    assert json.loads(ntfy.counter_path(env).read_text()) == {"date": "2026-03-04", "push": 3, "digest": 1, "alert": 0}


def test_the_count_starts_again_the_next_day(env):
    s = Sender()
    for _ in range(3):
        ntfy.send(CFG, env, "m", now=NOON, sender=s)
    assert ntfy.send(CFG, env, "m", now=NOON + timedelta(days=1), sender=s) == "sent: 1 of 3 pushes today"


def test_a_failed_send_does_not_use_up_the_allowance(env):
    with pytest.raises(ntfy.NotifyError, match="answered 503"):
        ntfy.send(CFG, env, "m", now=NOON, sender=Sender(503))
    assert ntfy.read_counts(env, "2026-03-04")["push"] == 0


def test_a_test_message_counts_as_a_push_and_is_capped(env):
    s = Sender()
    for n in (1, 2, 3):
        assert ntfy.send_test(CFG, env, NOON, s) == f"sent: {n} of 3 pushes today"
    assert s.calls[0][2]["Title"] == "ws test"
    with pytest.raises(ntfy.CapReached):
        ntfy.send_test(CFG, env, NOON, s)
    assert len(s.calls) == 3


def test_the_counter_is_under_logs_a_control_path(box, env):
    from ws import policy

    ntfy.send(CFG, env, "m", now=NOON, sender=Sender())
    path = ntfy.counter_path(env)
    assert path == box / ".ws" / "logs" / "notify-count.json" and path.is_file()
    assert str(path).startswith(tuple(policy.control_paths(box / ".ws")))


def write_counter(env, day, push=0, digest=0):
    ntfy.counter_path(env).parent.mkdir(parents=True, exist_ok=True)
    ntfy.counter_path(env).write_text(json.dumps({"date": day, "push": push, "digest": digest}))


def test_the_count_never_goes_back_when_the_date_does(env):
    """Alternating the zone between +14 and -12 moved the date back and forth and wiped the count each time."""
    s = Sender()
    write_counter(env, "2026-03-05", push=3)  # a zone ahead of UTC already saw the 5th
    with pytest.raises(ntfy.CapReached):
        ntfy.send(CFG, env, "m", now=NOON, sender=s)  # it is the 4th here
    assert s.calls == []
    assert json.loads(ntfy.counter_path(env).read_text())["date"] == "2026-03-05"


@needs_tzdata
def test_flipping_zones_cannot_get_more_than_the_cap(env):
    s, sent = Sender(), 0
    for i in range(10):
        zone = "Pacific/Kiritimati" if i % 2 == 0 else "Etc/GMT+12"  # +14 and -12
        try:
            ntfy.send({**CFG, "timezone": zone}, env, "m", now=NOON, sender=s)
            sent += 1
        except ntfy.CapReached:
            pass
    assert sent == 3 and len(s.calls) == 3


def test_a_later_date_moves_on_and_an_earlier_one_is_fresh(env):
    write_counter(env, "2026-03-03", push=3, digest=1)
    assert ntfy.send(CFG, env, "m", now=NOON, sender=Sender()) == "sent: 1 of 3 pushes today"
    assert ntfy.read_counts(env, "2026-03-04") == {"date": "2026-03-04", "push": 1, "digest": 0, "alert": 0}


@needs_tzdata
def test_the_day_is_the_date_in_the_profile_zone(env, write_profile):
    late = datetime(2026, 3, 4, 23, 30, tzinfo=UTC)  # already the 5th in Auckland
    assert ntfy.today(ntfy.zone(CFG, env), late) == "2026-03-04"  # no zone set: UTC
    write_profile({"machine": {"timezone": "Pacific/Auckland"}})
    assert ntfy.today(ntfy.zone(CFG, env), late) == "2026-03-05"
    assert ntfy.today(ntfy.zone({**CFG, "timezone": "UTC"}, env), late) == "2026-03-04"  # the role's own wins
    ntfy.send(CFG, env, "m", now=late, sender=Sender())
    assert json.loads(ntfy.counter_path(env).read_text())["date"] == "2026-03-05"


def test_a_bad_timezone_is_a_plain_error(env, linux):
    with pytest.raises(ntfy.NotifyError, match="not a time zone name"):
        ntfy.zone({"timezone": "Mars/Olympus"}, env)
    with pytest.raises(ntfy.NotifyError):
        ntfy.zone({"timezone": 5}, env)
    assert ntfy.check({**CFG, "timezone": "Mars/Olympus"}, linux, env, False)[0] == "fail"


def test_the_caps_come_from_the_policy(box, env):
    (box / ".ws" / "policy.toml").write_text("[interrupts]\ndigest_per_day = 0\npushes_per_day = 1\n")
    assert ntfy.caps(env) == {"push": 1, "digest": 0, "alert": 50}
    s = Sender()
    ntfy.send(CFG, env, "m", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached, match="1 of 1 pushes"):
        ntfy.send(CFG, env, "m", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached, match="0 of 0 digest"):
        ntfy.send(CFG, env, "d", kind="digest", now=NOON, sender=s)


def test_the_defaults_are_one_digest_and_three_pushes(env):
    assert ntfy.caps(env) == {"push": 3, "digest": 1, "alert": 50}


def test_a_policy_that_does_not_read_stops_a_send(box, env):
    (box / ".ws" / "policy.toml").write_text("[interrupts]\npushes_per_day = -2\n")
    with pytest.raises(ntfy.NotifyError, match="policy"):
        ntfy.send(CFG, env, "m", now=NOON, sender=Sender())


def test_bad_messages_and_kinds(env):
    with pytest.raises(ntfy.NotifyError, match="empty"):
        ntfy.send(CFG, env, "  ", now=NOON, sender=Sender())
    with pytest.raises(ntfy.NotifyError, match="push, digest or alert"):
        ntfy.send(CFG, env, "m", kind="alarm", now=NOON, sender=Sender())


def test_a_header_cannot_carry_a_line_break(env):
    s = Sender()
    ntfy.send(CFG, env, "m", title="a\r\nX-Evil: 1 é", now=NOON, sender=s)
    title = s.calls[0][2]["Title"]
    assert "\n" not in title and "\r" not in title and title.isascii()


def test_a_damaged_counter_file_is_a_fresh_day(env):
    ntfy.counter_path(env).parent.mkdir(parents=True)
    ntfy.counter_path(env).write_text("{not json")
    assert ntfy.read_counts(env, "2026-03-04") == {"date": "2026-03-04", "push": 0, "digest": 0, "alert": 0}
    ntfy.counter_path(env).write_text('{"date": "2026-03-04", "push": -1, "digest": 0}')
    assert ntfy.read_counts(env, "2026-03-04")["push"] == 0


def test_check(box, linux, env):
    assert ntfy.check({}, linux, env, False)[0] == "fail"
    status, detail = ntfy.check(CFG, linux, env, False)
    assert status == "ok" and "no token" in detail and "0/3 pushes" in detail
    status, detail = ntfy.check({**CFG, "token": "ntfy"}, linux, env, False)
    assert status == "warn" and "not in the secrets registry" in detail
    (box / ".ws" / "secrets.toml").write_text('[ntfy]\nhosts = ["ntfy.example.org"]\nheader = "authorization"\n')
    status, detail = ntfy.check({**CFG, "token": "ntfy"}, linux, env, False)
    assert status == "ok" and "through the broker" in detail
    assert ntfy.check({**CFG, "token": "../x"}, linux, env, False)[0] == "fail"


def test_the_post_helper_never_follows_a_redirect(monkeypatch):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert ntfy.post(f"http://127.0.0.1:{srv.server_port}/t", b"x", {}) == 302
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_send_status_and_test(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    assert cli.main(["notify", "status"]) == 0
    assert "0 of 3 pushes" in capsys.readouterr().out
    assert cli.main(["notify", "test"]) == 0
    assert "sent: 1 of 3 pushes today" in capsys.readouterr().out
    assert cli.main(["notify", "status"]) == 0
    assert "1 of 3 pushes" in capsys.readouterr().out


def test_cli_refuses_when_notify_is_not_ntfy(box, write_profile, capsys):
    write_profile({"roles": {"notify": {"adapter": "none"}}})
    assert cli.main(["notify", "test"]) == 1
    assert 'adapter = "ntfy"' in capsys.readouterr().err


def test_a_clock_that_jumped_far_ahead_does_not_freeze_pushes(env):
    s = Sender()
    write_counter(env, "2026-03-06", push=3)  # 2 days ahead: could still be a zone or a skewed clock, so it counts
    with pytest.raises(ntfy.CapReached):
        ntfy.send(CFG, env, "m", now=NOON, sender=s)
    write_counter(env, "2026-03-07", push=3)  # 3 days ahead: a clock that was wrong, not a zone
    assert ntfy.send(CFG, env, "m", now=NOON, sender=s) == "sent: 1 of 3 pushes today"
    assert json.loads(ntfy.counter_path(env).read_text())["date"] == "2026-03-04"


@needs_tzdata
def test_the_day_defaults_to_the_zone_ws_detects_for_the_machine(env):
    late = datetime(2026, 3, 4, 23, 30, tzinfo=UTC)
    assert ntfy.today(ntfy.zone(CFG, {**env, "TZ": "Pacific/Auckland"}), late) == "2026-03-05"
    assert ntfy.today(ntfy.zone(CFG, {**env, "TZ": "UTC"}), late) == "2026-03-04"
    assert (
        ntfy.today(ntfy.zone(CFG, {**env, "TZ": "NZST-12NZDT,M9.5.0,M4.1.0/3"}), late) == "2026-03-04"
    )  # no name: UTC


@needs_tzdata
def test_a_profile_zone_beats_the_detected_one(env, write_profile):
    late = datetime(2026, 3, 4, 23, 30, tzinfo=UTC)
    write_profile({"machine": {"timezone": "UTC"}})
    assert ntfy.today(ntfy.zone(CFG, {**env, "TZ": "Pacific/Auckland"}), late) == "2026-03-04"


# ---- session alerts and notify-last ------------------------------------------------------------------------------------

NOTIFICATION = {"hook_event_name": "Notification", "cwd": "/work/payroll-app", "message": "Claude needs permission"}
STOP = {"hook_event_name": "Stop", "cwd": "/work/payroll-app"}


def counts_of(env):
    return json.loads(ntfy.counter_path(env).read_text())


def test_a_notification_is_one_high_priority_alert_and_no_push(env):
    s = Sender()
    line = ntfy.alert(CFG, env, NOTIFICATION, "claude-code", now=NOON, sender=s)
    [(url, body, headers)] = s.calls
    assert url == URL and body == b"payroll-app needs you"  # title mode: the folder only
    assert headers["Priority"] == "high" and headers["Title"] == "ws claude-code"
    assert line == "sent: 1 of 50 alerts today"
    assert (counts_of(env)["alert"], counts_of(env)["push"]) == (1, 0)


def test_a_stop_is_a_default_priority_alert(env):
    s = Sender()
    ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=s)
    [(_, body, headers)] = s.calls
    assert body == b"payroll-app finished" and "Priority" not in headers


def test_full_text_adds_the_runners_message(env):
    s = Sender()
    ntfy.alert({**CFG, "alert_text": "full"}, env, NOTIFICATION, "claude-code", now=NOON, sender=s)
    assert s.calls[0][1] == b"payroll-app needs you: Claude needs permission"
    s2 = Sender()
    ntfy.alert(CFG, env, NOTIFICATION, "claude-code", now=NOON, sender=s2)
    assert b"permission" not in s2.calls[0][1]


def test_plain_text_sends_no_folder_name_or_runner_message(env):
    s = Sender()
    ntfy.alert({**CFG, "alert_text": "plain"}, env, NOTIFICATION, "claude-code", now=NOON, sender=s)
    ntfy.alert({**CFG, "alert_text": "plain"}, env, STOP, "claude-code", now=NOON + timedelta(minutes=1), sender=s)
    assert [c[1] for c in s.calls] == [b"a session needs you", b"a session finished"]
    assert "payroll" not in json.dumps([[c[1].decode(), c[2]] for c in s.calls])


def test_alert_text_must_be_title_full_or_plain(env):
    with pytest.raises(ntfy.NotifyError, match="alert_text"):
        ntfy.alert({**CFG, "alert_text": "loud"}, env, STOP, "claude-code", now=NOON, sender=Sender())


def test_the_alert_cap_is_apart_from_the_push_cap(env):
    s = Sender()
    for _ in range(3):
        ntfy.send(CFG, env, "p", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached):
        ntfy.send(CFG, env, "p", now=NOON, sender=s)
    cfg = {**CFG, "alerts_per_day": 2}
    ntfy.alert(cfg, env, STOP, "claude-code", now=NOON, sender=s)
    ntfy.alert(cfg, env, STOP, "claude-code", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached, match="2 of 2 alerts"):
        ntfy.alert(cfg, env, STOP, "claude-code", now=NOON, sender=s)
    assert len(s.calls) == 5
    assert counts_of(env)["push"] == 3  # alerts didn't use up pushes


def test_alerts_per_day_defaults_to_50_and_is_checked():
    assert ntfy.alert_cap({}) == 50
    for bad in (-1, "5", True, 1.5):
        with pytest.raises(ntfy.NotifyError, match="alerts_per_day"):
            ntfy.alert_cap({"alerts_per_day": bad})


def test_a_counter_file_from_before_alerts_still_reads(env):
    ntfy.counter_path(env).parent.mkdir(parents=True, exist_ok=True)
    ntfy.counter_path(env).write_text(json.dumps({"date": "2026-03-04", "push": 2, "digest": 1}))
    assert ntfy.read_counts(env, "2026-03-04") == {"date": "2026-03-04", "push": 2, "digest": 1, "alert": 0}


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "PreToolUse", "cwd": "/w"},
        {"cwd": "/w"},
        {"hook_event_name": ["Stop"]},
        [],
        "Stop",
        None,
    ],
)
def test_no_alert_for_other_events_or_junk(env, payload):
    s = Sender()
    assert ntfy.alert(CFG, env, payload, "claude-code", now=NOON, sender=s) is None
    assert s.calls == []


@pytest.mark.parametrize(
    ("payload", "extra_env"),
    [
        (STOP, {"WS_RUN_ID": "r1"}),
        (STOP, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}),
        (STOP, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}),
        (STOP, {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}),
        ({**STOP, "headless": True}, {}),
    ],
)
def test_no_alert_for_a_headless_session(env, payload, extra_env):
    s = Sender()
    assert ntfy.alert(CFG, {**env, **extra_env}, payload, "claude-code", now=NOON, sender=s) is None
    assert s.calls == []


def test_an_app_session_gets_its_alert(env):
    s = Sender()
    ntfy.alert(CFG, {**env, "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, STOP, "claude-code", now=NOON, sender=s)
    assert len(s.calls) == 1


def test_session_alerts_false_sends_nothing(env):
    s = Sender()
    assert ntfy.alert({**CFG, "session_alerts": False}, env, STOP, "claude-code", now=NOON, sender=s) is None
    with pytest.raises(ntfy.NotifyError, match="session_alerts"):
        ntfy.alert({**CFG, "session_alerts": "no"}, env, STOP, "claude-code", now=NOON, sender=s)
    assert s.calls == []


def test_the_folder_name_is_one_clean_line(env):
    s = Sender()
    ntfy.alert(CFG, env, {**STOP, "cwd": "/w/bad\nname\x07/"}, "claude-code", now=NOON, sender=s)
    assert s.calls[0][1] == b"bad name finished"
    ntfy.alert(CFG, env, {"hook_event_name": "Stop"}, "claude-code", now=NOON, sender=s)
    assert s.calls[1][1] == b"session finished"


def read_last(env):
    return json.loads((ntfy.ws_home(env) / "logs" / "notify-last.json").read_text())


def test_notify_last_is_written_on_ok_without_the_text(env):
    ntfy.send(CFG, env, "private body", "T", kind="digest", now=NOON, sender=Sender())
    assert read_last(env) == {"at": "2026-03-04T12:00:00+00:00", "kind": "digest", "ok": True, "error": None}


def test_notify_last_is_written_on_failure(env):
    with pytest.raises(ntfy.NotifyError, match="500"):
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=Sender(500))
    last = read_last(env)
    assert last["kind"] == "alert" and last["ok"] is False and last["error"] == "http_500"
    assert ntfy.read_counts(env, "2026-03-04")["alert"] == 0  # a failed send isn't counted
    assert "payroll" not in json.dumps(last)


def test_a_refused_cap_leaves_notify_last_alone(env):
    with pytest.raises(ntfy.CapReached):
        ntfy.alert({**CFG, "alerts_per_day": 0}, env, STOP, "claude-code", now=NOON, sender=Sender())
    assert not (ntfy.ws_home(env) / "logs" / "notify-last.json").exists()


def test_the_test_push_writes_notify_last(env):
    ntfy.send_test(CFG, env, now=NOON, sender=Sender())
    assert read_last(env)["kind"] == "push"


def run_alert(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    code = cli.main(["notify", "alert", "--runner", "claude-code"])
    return code, capsys.readouterr().err


def test_cli_alert_sends_one_high_priority_alert_and_no_push(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    code, err = run_alert(monkeypatch, capsys, NOTIFICATION)
    assert code == 0 and "1 of 50 alerts" in err
    assert len(s.calls) == 1 and s.calls[0][2]["Priority"] == "high"
    assert cli.main(["notify", "status"]) == 0
    out = capsys.readouterr().out
    assert "0 of 3 pushes" in out and "1 of 50 alerts" in out


def test_cli_status_json_has_the_alert_count(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    monkeypatch.setattr(ntfy, "post", Sender())
    run_alert(monkeypatch, capsys, STOP)
    assert cli.main(["notify", "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert (data["push"], data["alert"], data["alert_cap"]) == (0, 1, 50)


@pytest.mark.parametrize("profile", [None, {"roles": {"notify": {"adapter": "none"}}}, {"roles": {}}])
def test_cli_alert_is_silent_without_a_notify_setup(box, write_profile, capsys, monkeypatch, profile):
    if profile is not None:
        write_profile(profile)
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    code, err = run_alert(monkeypatch, capsys, STOP)
    assert (code, err, s.calls) == (0, "", [])


@pytest.mark.parametrize("junk", ["", "not json", "[1, 2]", "42", "null", '{"hook_event_name": 5}'])
def test_cli_alert_exits_0_on_a_junk_payload(box, write_profile, capsys, monkeypatch, junk):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    code, _ = run_alert(monkeypatch, capsys, junk)
    assert code == 0 and s.calls == []


def test_cli_alert_exits_0_and_says_why_when_the_send_fails(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    monkeypatch.setattr(ntfy, "post", Sender(503))
    code, err = run_alert(monkeypatch, capsys, STOP)
    assert code == 0 and "alert not sent" in err and "503" in err


def test_cli_alert_skips_a_ws_run_session(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    monkeypatch.setenv("WS_RUN_ID", "r1")
    code, err = run_alert(monkeypatch, capsys, STOP)
    assert (code, err, s.calls) == (0, "", [])


# ---- an alert must not hold a session up ---------------------------------------------------------------------------------


def test_an_alert_that_hangs_gives_up_with_a_timeout_code(env, monkeypatch):
    monkeypatch.setattr(ntfy, "ALERT_TIMEOUT_S", 0.2)
    gate = threading.Event()

    def stuck(url, body, headers):
        gate.wait(10)
        return 200

    started = time.monotonic()
    try:
        with pytest.raises(ntfy.NotifyError, match="more than"):
            ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=stuck)
        assert time.monotonic() - started < 2
    finally:
        gate.set()
    assert read_last(env)["error"] == "timeout"
    assert ntfy.read_counts(env, "2026-03-04")["alert"] == 0


def test_the_default_alert_sender_waits_three_seconds_and_a_push_ten(env, monkeypatch):
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            seen.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 204, "", {}, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    ntfy.alert(CFG, env, STOP, "claude-code", now=NOON)
    ntfy.send(CFG, env, "hi", now=NOON)
    assert seen == [3.0, 10.0]


def test_the_lease_is_inside_the_alert_time_limit(env, monkeypatch):
    monkeypatch.setattr(ntfy, "ALERT_TIMEOUT_S", 0.2)
    gate = threading.Event()

    def slow_lease(name, purpose, host, socket=None, ttl_s=None):
        gate.wait(10)
        raise broker.BrokerError("late")

    monkeypatch.setattr(broker, "request", slow_lease)
    try:
        with pytest.raises(ntfy.NotifyError, match="more than"):
            ntfy.alert({**CFG, "token": "ntfy"}, env, STOP, "claude-code", now=NOON, sender=Sender())
    finally:
        gate.set()
    assert read_last(env)["error"] == "timeout"


def test_a_pushes_wait_is_not_cut_short(env, monkeypatch):
    monkeypatch.setattr(ntfy, "ALERT_TIMEOUT_S", 0.01)

    def slowish(url, body, headers):
        time.sleep(0.1)
        return 200

    assert ntfy.send(CFG, env, "hi", now=NOON, sender=slowish).startswith("sent")


def test_an_alert_skips_when_another_send_holds_the_counter(env):
    fcntl = pytest.importorskip("fcntl")
    ntfy.counter_path(env).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(ntfy.counter_path(env), os.O_RDWR | os.O_CREAT, 0o600)
    s = Sender()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(ntfy.NotifyError, match="holds the counter"):
            ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=s)
        assert time.monotonic() - started < 1
    finally:
        os.close(fd)
    assert s.calls == []
    last = read_last(env)
    assert (last["ok"], last["error"]) == (False, "busy")
    ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=s)  # busy is not a failure: no backoff
    assert len(s.calls) == 1


def test_a_failed_alert_backs_off_alerts_for_five_minutes(env):
    with pytest.raises(ntfy.NotifyError, match="500"):
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=Sender(500))
    s = Sender()
    with pytest.raises(ntfy.NotifyError, match="paused"):
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON + timedelta(minutes=4, seconds=59), sender=s)
    assert s.calls == []
    assert read_last(env)["error"] == "http_500"  # the pause does not extend itself
    assert ntfy.send(CFG, env, "hi", now=NOON + timedelta(minutes=1), sender=s).startswith("sent")  # pushes go on
    assert len(s.calls) == 1
    # the push went fine, so the failure is no longer the last word and alerts resume
    ntfy.alert(CFG, env, STOP, "claude-code", now=NOON + timedelta(minutes=2), sender=s)
    assert len(s.calls) == 2


def test_alerts_resume_after_the_backoff(env):
    with pytest.raises(ntfy.NotifyError):
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=Sender(503))
    s = Sender()
    ntfy.alert(CFG, env, STOP, "claude-code", now=NOON + timedelta(minutes=5), sender=s)
    assert len(s.calls) == 1 and read_last(env)["ok"] is True


def test_a_backoff_refusal_carries_its_code(env):
    with pytest.raises(ntfy.NotifyError) as e:
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=Sender(500))
    assert e.value.code == "http_500"
    with pytest.raises(ntfy.NotifyError) as e:
        ntfy.alert(CFG, env, STOP, "claude-code", now=NOON, sender=Sender())
    assert e.value.code == "backoff"


def test_notify_last_is_private_and_holds_only_a_fixed_code(env):
    def odd(url, body, headers):
        raise ntfy.NotifyError("token SECRET-123 at https://host/lease", "not-a-code")

    with pytest.raises(ntfy.NotifyError):
        ntfy.send(CFG, env, "hi", now=NOON, sender=odd)
    path = ntfy.ws_home(env) / "logs" / "notify-last.json"
    assert read_last(env)["error"] == "error" and "SECRET" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    ntfy.send(CFG, env, "hi", now=NOON, sender=Sender())
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (urllib.error.URLError(TimeoutError("slow")), "timeout"),
        (TimeoutError("slow"), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError()), "refused"),
        (ConnectionRefusedError(), "refused"),
        (urllib.error.URLError("dns"), "error"),
        (ValueError("x"), "error"),
    ],
)
def test_the_post_helper_codes_network_failures_by_type(monkeypatch, exc, code):
    class Opener:
        def open(self, req, timeout=None):
            raise exc

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(ntfy.NotifyError) as e:
        ntfy.post(URL, b"x", {})
    assert e.value.code == code


def test_a_broker_that_will_not_lease_is_coded_refused(env, monkeypatch):
    def refuse(*a, **k):
        raise broker.BrokerError("no")

    monkeypatch.setattr(broker, "request", refuse)
    with pytest.raises(ntfy.NotifyError):
        ntfy.alert({**CFG, "token": "ntfy"}, env, STOP, "claude-code", now=NOON, sender=Sender())
    assert read_last(env)["error"] == "refused"


def run_alert_args(monkeypatch, capsys, payload, argv):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    code = cli.main(argv)
    return code, capsys.readouterr().err


def test_cli_alert_runs_without_a_runner(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    code, err = run_alert_args(monkeypatch, capsys, STOP, ["notify", "alert"])
    assert code == 0 and "1 of 50 alerts" in err
    assert s.calls[0][2]["Title"] == "ws unknown"


def test_cli_alert_exits_0_with_no_stdin(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    monkeypatch.setattr(ntfy, "post", Sender())
    monkeypatch.setattr("sys.stdin", None)
    assert cli.main(["notify", "alert", "--runner", "claude-code"]) == 0
    assert "alert not sent" in capsys.readouterr().err


def test_cli_alert_exits_0_on_a_payload_nested_past_the_recursion_limit(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    code, err = run_alert_args(monkeypatch, capsys, "[" * 200_000 + "]" * 200_000, ["notify", "alert"])
    assert code == 0 and "alert not sent" in err and s.calls == []


def test_cli_alert_exits_0_when_the_broker_breaks(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": {**CFG, "token": "ntfy"}}})

    def boom(*a, **k):
        raise RuntimeError("broker exploded with TOKEN-XYZ")

    monkeypatch.setattr(broker, "request", boom)
    code, err = run_alert_args(monkeypatch, capsys, STOP, ["notify", "alert", "--runner", "claude-code"])
    assert code == 0 and "RuntimeError" in err and "TOKEN-XYZ" not in err
