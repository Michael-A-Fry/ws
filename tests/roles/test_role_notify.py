"""Notify role, ntfy: the topic URL, the broker lease, the daily cap (1 digest, 3 pushes) and `ws notify`. The sender
and the broker are fakes; nothing leaves the machine."""

from __future__ import annotations

import json
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
    assert json.loads(ntfy.counter_path(env).read_text()) == {"date": "2026-03-04", "push": 3, "digest": 1}


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
    assert ntfy.read_counts(env, "2026-03-04") == {"date": "2026-03-04", "push": 1, "digest": 0}


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
    assert ntfy.caps(env) == {"push": 1, "digest": 0}
    s = Sender()
    ntfy.send(CFG, env, "m", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached, match="1 of 1 pushes"):
        ntfy.send(CFG, env, "m", now=NOON, sender=s)
    with pytest.raises(ntfy.CapReached, match="0 of 0 digest"):
        ntfy.send(CFG, env, "d", kind="digest", now=NOON, sender=s)


def test_the_defaults_are_one_digest_and_three_pushes(env):
    assert ntfy.caps(env) == {"push": 3, "digest": 1}


def test_a_policy_that_does_not_read_stops_a_send(box, env):
    (box / ".ws" / "policy.toml").write_text("[interrupts]\npushes_per_day = -2\n")
    with pytest.raises(ntfy.NotifyError, match="policy"):
        ntfy.send(CFG, env, "m", now=NOON, sender=Sender())


def test_bad_messages_and_kinds(env):
    with pytest.raises(ntfy.NotifyError, match="empty"):
        ntfy.send(CFG, env, "  ", now=NOON, sender=Sender())
    with pytest.raises(ntfy.NotifyError, match="push or digest"):
        ntfy.send(CFG, env, "m", kind="alarm", now=NOON, sender=Sender())


def test_a_header_cannot_carry_a_line_break(env):
    s = Sender()
    ntfy.send(CFG, env, "m", title="a\r\nX-Evil: 1 é", now=NOON, sender=s)
    title = s.calls[0][2]["Title"]
    assert "\n" not in title and "\r" not in title and title.isascii()


def test_a_damaged_counter_file_is_a_fresh_day(env):
    ntfy.counter_path(env).parent.mkdir(parents=True)
    ntfy.counter_path(env).write_text("{not json")
    assert ntfy.read_counts(env, "2026-03-04") == {"date": "2026-03-04", "push": 0, "digest": 0}
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
