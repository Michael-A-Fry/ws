"""Queue admission, part two: memory held for others, one notice per stop, and children that spend from a parent's
reservation. The ledger is driven with the injected clock and host of tests/test_queue.py; the notify sender is a fake,
so nothing leaves the machine."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_queue import T0, Clock, Host, call, make, req, saw, spend, write_job
from test_queue import box as queue_box  # noqa: F401  (the scratch home and fake claude of test_queue.py)

from ws import cli, jobs, queue, run, runrecord
from ws.profile import dumps

NTFY = {"adapter": "ntfy", "url": "https://ntfy.example.org/my-topic"}


@pytest.fixture
def home(queue_box):  # noqa: F811 (a fixture takes the other as its argument)
    return queue_box


class Sender:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, body, headers):
        self.calls.append((url, body.decode(), headers))
        return self.status


# ---- memory held for others -------------------------------------------------------------------------------------------


def test_memory_reserved_gb_is_taken_off_what_is_available_before_the_floor(tmp_path):
    host = Host(avail=4.0, total=16.0)  # the floor is 2.4 GB (15% of 16)
    assert queue.Limits().memory_reserved_gb == 0.0
    plain = make(tmp_path / "a", host=host)
    assert plain.admit(req(ram_gb=1.5), T0, spend(), dry=True).kind == "admit"  # 4 - 1.5 = 2.5
    held = make(tmp_path / "b", host=host, limits=queue.Limits(busy_s=0.2, memory_reserved_gb=1.0))
    d = held.admit(req(ram_gb=1.5), T0, spend(), dry=True)  # (4 - 1) - 1.5 = 1.5, under the floor
    assert d.kind == "wait" and d.counter == "memory" and "1 GB held for others" in d.reason
    assert held.admit(req(ram_gb=0.5), T0, spend(), dry=True).kind == "admit"  # 3 - 0.5 = 2.5
    # a job that declared no memory is not held up by it, and a host that cannot report memory still admits
    assert held.admit(req(), T0, spend(), dry=True).kind == "admit"


def test_memory_reserved_gb_is_checked_like_the_other_limits():
    got = queue.Limits.from_profile({"roles": {"queue": {"memory_reserved_gb": 6}}})
    assert got.memory_reserved_gb == 6.0
    assert queue.Limits.from_profile({"roles": {"queue": {"memory_reserved_gb": 0}}}).memory_reserved_gb == 0.0
    for bad in (-1, "4", True, float("inf"), float("nan")):
        with pytest.raises(queue.LimitsError, match="memory_reserved_gb") as e:
            queue.Limits.from_profile({"roles": {"queue": {"memory_reserved_gb": bad}}})
        assert e.value.exit_code == 2


# ---- one notice per stop ----------------------------------------------------------------------------------------------


def test_notify_stop_sends_one_short_line_through_the_notify_role(tmp_path):
    s = Sender()
    prof = {"roles": {"notify": NTFY}}
    assert queue.notify_stop("nightly", "r1", "refused:profile_cap", prof=prof, sender=s, now=T0) is True
    [(url, body, headers)] = s.calls
    assert url == NTFY["url"] and body == "nightly stopped: refused:profile_cap" and headers["Title"] == "ws job"
    # a person who keeps folder names off the topic keeps job names off it as well
    s2 = Sender()
    queue.notify_stop(
        "nightly", "r1", "timeout", prof={"roles": {"notify": {**NTFY, "alert_text": "plain"}}}, sender=s2, now=T0
    )
    assert s2.calls[0][1] == "a job stopped: timeout"


def test_notify_stop_is_silent_without_a_notify_role_and_never_raises(tmp_path):
    s = Sender()
    for prof in ({}, {"roles": {}}, {"roles": {"notify": {"adapter": "none"}}}, {"roles": {"notify": "ntfy"}}):
        assert queue.notify_stop("j", "r", "x", prof=prof, sender=s, now=T0) is False
    assert s.calls == []
    # a server that answers with an error, a sender that blows up and a topic that is not a URL change nothing
    prof = {"roles": {"notify": NTFY}}
    assert queue.notify_stop("j", "r", "x", prof=prof, sender=Sender(status=500), now=T0) is False

    def boom(url, body, headers):
        raise OSError("down")

    assert queue.notify_stop("j", "r", "x", prof=prof, sender=boom, now=T0) is False
    bad = {"roles": {"notify": {"adapter": "ntfy", "url": "nope"}}}
    assert queue.notify_stop("j", "r", "x", prof=bad, sender=s, now=T0) is False


def test_notify_stop_with_no_profile_given_reads_the_one_the_environment_names(home):
    assert queue.notify_stop("j", "r", "x", now=T0) is False  # the profile of the scratch home has no notify role


def test_a_retry_is_not_a_stop_but_giving_up_is_one_notice(tmp_path):
    clock = Clock()
    told = []
    ledger = make(tmp_path, clock=clock, on_stop=lambda *a: told.append(a))
    ledger.enqueue("j", None, T0)
    attempts = []

    def runner(item):
        attempts.append(item.run_id)
        return queue.Outcome(1, "error", "timeout")

    for _ in range(queue.MAX_RETRIES + 2):
        queue.drain(ledger, runner)
        clock.advance(seconds=queue.backoff_s(queue.MAX_RETRIES) + 1)
    assert len(attempts) == queue.MAX_RETRIES + 1  # the first try and three retries
    assert told == [("j", attempts[-1], "timeout")]  # one line, for the last attempt only


def test_a_waiting_run_and_a_finished_one_say_nothing_and_a_refused_one_says_its_reason(tmp_path):
    told = []
    ledger = make(tmp_path, on_stop=lambda *a: told.append(a))
    ledger.enqueue("waits", None, T0)
    queue.drain(ledger, lambda item: queue.Outcome(3, None, None, "concurrency"))
    ledger.enqueue("fine", None, T0)
    queue.drain(ledger, lambda item: queue.Outcome(0, "ok", "completed"), limit=1)
    assert told == []
    ledger.enqueue("over", None, T0 + timedelta(seconds=1))
    queue.drain(ledger, lambda item: queue.Outcome(3, "refused", "refused:profile_cap"))
    assert [(t[0], t[2]) for t in told] == [("over", "refused:profile_cap")]


def test_a_notice_that_fails_changes_nothing_about_the_job(tmp_path):
    def boom(job, run_id, reason):
        raise RuntimeError("down")

    ledger = make(tmp_path, on_stop=boom)
    ledger.enqueue("j", None, T0)
    done = queue.drain(ledger, lambda item: queue.Outcome(1, "error", "runner_error"))
    assert [d["action"] for d in done] == ["fail"]
    assert ledger.receipts(last=5)[0]["status"] == "error"


def test_a_slot_that_is_refused_or_times_out_says_so_once(tmp_path):
    clock = Clock()
    told = []
    ledger = make(
        tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5, busy_s=0.2), on_stop=lambda *a: told.append(a)
    )
    lane = {"lane": "ui", "who": "writer", "usd": 0.0, "turns": 0, "seconds": 0.0, "cap_usd": None}
    ledger.admit(req(job="holder", **lane), clock(), spend())
    waiter = req(job="waiter", **lane)
    with pytest.raises(queue.SlotTimeout):
        queue.wait_for_slot(ledger, waiter, timeout_s=8, sleep=lambda s: clock.advance(seconds=s))
    never = req(job="never", ram_gb=99.0, **lane)
    with pytest.raises(queue.SlotRefused):
        queue.wait_for_slot(ledger, never, sleep=lambda s: None)
    assert [(t[0], t[2]) for t in told] == [("waiter", "wait_timeout"), ("never", "refused:memory_never")]


def test_a_run_cut_short_is_not_announced_and_a_failed_one_is_announced_after_it_settles(home, monkeypatch):
    from ws.roles import ntfy

    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    (home / "profile.toml").write_text(
        dumps({"roles": {"budget": {"monthly_cap_usd": 20}, "notify": NTFY}}), encoding="utf-8"
    )
    write_job(home, "cut", prompt="term")  # the runner dies on SIGTERM: a preempted run
    assert cli.main(["run", "cut"]) == 1
    assert runrecord.read(runrecord.runs_path())[-1]["status"] == "preempted" and s.calls == []
    for rec in (
        {"job": "j", "run_id": "r", "status": "error", "stop_reason": "interrupted"},
        {"job": "j", "run_id": "r", "status": "preempted", "stop_reason": "preempted"},
        {"job": "j", "run_id": "r", "status": "ok", "stop_reason": "completed"},
    ):
        token = run._says_stops.set(True)
        try:
            run._announce_stop(rec)
        finally:
            run._says_stops.reset(token)
    assert s.calls == []
    order = []
    real_settle = run._settle
    monkeypatch.setattr(run, "_settle", lambda *a, **k: (order.append("settle"), real_settle(*a, **k)))
    monkeypatch.setattr(queue, "notify_stop", lambda *a, **k: order.append("notify"))
    write_job(home, "bad", prompt="fail")
    assert cli.main(["run", "bad"]) == 1
    assert order == ["settle", "notify"]


def notify_box(home, monkeypatch, cap=20):
    """A scratch home whose profile has a notify role; the one thing that would leave the machine is faked."""
    s = Sender()
    from ws.roles import ntfy

    monkeypatch.setattr(ntfy, "post", s)
    (home / "profile.toml").write_text(
        dumps({"roles": {"budget": {"monthly_cap_usd": cap}, "notify": NTFY}}), encoding="utf-8"
    )
    return s


def test_ws_run_tells_you_once_when_it_fails_or_is_refused_and_not_when_it_succeeds(home, capsys, monkeypatch):
    s = notify_box(home, monkeypatch)
    write_job(home, "good")
    write_job(home, "bad", prompt="fail")
    assert cli.main(["run", "good"]) == 0
    assert s.calls == []
    assert cli.main(["run", "bad"]) == 1
    assert [c[1] for c in s.calls] == ["bad stopped: runner_error"]
    (home / "profile.toml").write_text(dumps({"roles": {"notify": NTFY}}), encoding="utf-8")  # no cap: refused
    assert cli.main(["run", "good"]) == 3
    assert len(s.calls) == 2 and s.calls[1][1].startswith("good stopped: refused:")
    capsys.readouterr()


def test_ws_run_with_no_notify_role_is_silent_and_its_outcome_is_unchanged(home, capsys, monkeypatch):
    from ws.roles import ntfy

    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    write_job(home, "bad", prompt="fail")
    assert cli.main(["run", "bad"]) == 1 and s.calls == []
    monkeypatch.setattr(ntfy, "post", Sender(status=500))  # a topic that answers 500 does not turn a 1 into anything
    (home / "profile.toml").write_text(
        dumps({"roles": {"budget": {"monthly_cap_usd": 20}, "notify": NTFY}}), encoding="utf-8"
    )
    assert cli.main(["run", "bad"]) == 1
    capsys.readouterr()


def test_ws_queue_run_tells_you_when_it_gives_a_message_up(home, capsys, monkeypatch):
    s = notify_box(home, monkeypatch)
    write_job(home, "bad", prompt="fail")
    write_job(home, "good")
    call(capsys, "queue", "add", "bad")
    call(capsys, "queue", "add", "good")
    _, env = call(capsys, "queue", "run")
    assert sorted(a["action"] for a in env["data"]["attempts"]) == ["done", "fail"]
    assert [c[1] for c in s.calls] == ["bad stopped: runner_error"]


# ---- children spend from the parent's reservation ---------------------------------------------------------------------


def parent_of(ledger, usd=3.0, **kw):
    d = ledger.admit(req(job="parent", usd=usd, cap_usd=kw.pop("cap_usd", 100.0), **kw), T0, spend())
    assert d.kind == "admit"
    return d.reservation


def child(parent, **kw):
    return req(parent_run_id=parent.run_id, **kw)


def test_a_child_is_checked_against_what_the_parent_reserved_less_its_other_children(tmp_path):
    ledger = make(tmp_path)
    p = parent_of(ledger, usd=3.0)
    a = ledger.admit(child(p, usd=1.0), T0, spend())
    b = ledger.admit(child(p, usd=1.0), T0, spend())
    assert a.kind == b.kind == "admit" and "parent_budget" in a.reservation.counters
    assert "profile_cap" in a.reservation.counters  # and the month's cap, as for any run
    # 3 - 1 - 1 leaves 1: a child that asks for more is resized to it, one that says strict is refused
    big = ledger.admit(child(p, usd=2.0), T0, spend())
    assert big.kind == "admit" and big.reservation.resized and big.reservation.usd == pytest.approx(1.0)
    assert big.reservation.max_budget_usd == pytest.approx(1.0)
    gone = ledger.admit(child(p, usd=0.5), T0, spend())  # nothing left now
    assert gone.kind == "refuse" and gone.counter == "parent_budget" and gone.stop_reason == "refused:parent_budget"
    strict = make(tmp_path / "s")
    sp = parent_of(strict, usd=1.5)
    assert strict.admit(child(sp, usd=1.0), T0, spend()).kind == "admit"
    refused = strict.admit(child(sp, usd=1.0, strict=True), T0, spend())
    assert refused.kind == "refuse" and refused.counter == "parent_budget" and "strict" in refused.reason


def test_a_child_with_no_live_parent_is_refused_with_its_own_reason(tmp_path):
    ledger = make(tmp_path)
    nobody = ledger.admit(req(parent_run_id="20261108T000000Z-ffffff"), T0, spend())
    assert nobody.kind == "refuse" and nobody.stop_reason == "refused:parent_not_running"
    p = parent_of(ledger)
    close = ledger.settle(p, status="ok", usd=0.5, tokens=None, stop_reason="completed", now=T0)
    assert close
    after = ledger.admit(child(p), T0, spend())
    assert after.kind == "refuse" and after.counter == "parent_not_running"
    own = req()
    assert ledger.admit(queue.Request(**{**own.__dict__, "parent_run_id": own.run_id}), T0, spend()).kind == "refuse"


def test_naming_a_parent_never_gets_a_run_past_the_monthly_cap(tmp_path):
    ledger = make(tmp_path)
    p = parent_of(ledger, usd=3.0, cap_usd=5.0)
    # the parent's own spend is not known while it runs, so its reservation cannot stand for the month: the child counts
    # beside it (US$3 + US$1 now held, the same dollars twice while both are live, and that is accepted)
    first = ledger.admit(child(p, usd=1.0, cap_usd=5.0), T0, spend())
    assert first.kind == "admit" and {"profile_cap", "parent_budget"} <= set(first.reservation.counters)
    # the parent has US$2 left for children, the month only US$1: the smaller room wins
    second = ledger.admit(child(p, usd=2.0, cap_usd=5.0), T0, spend())
    assert second.reservation.resized and second.reservation.usd == pytest.approx(1.0)
    # the month is now full (3 + 1 + 1 of 5); the parent still has room, and the child is refused for the month
    third = ledger.admit(child(p, usd=0.5, cap_usd=5.0), T0, spend())
    assert third.kind == "refuse" and third.stop_reason == "refused:profile_cap"
    # and a month already spent in the log refuses a child whose parent has plenty of room
    ledger2 = make(tmp_path / "b")
    big = parent_of(ledger2, usd=3.0, cap_usd=10.0)
    over = ledger2.admit(child(big, usd=1.0, cap_usd=10.0), T0, spend(7.0))
    assert over.kind == "refuse" and over.counter == "profile_cap"


def test_a_child_of_another_purpose_meets_its_own_purpose_cap(tmp_path):
    ledger = make(tmp_path)
    p = parent_of(ledger, usd=3.0, purpose="a", purpose_cap_usd=50.0)
    kid = child(p, usd=1.0, purpose="b", purpose_cap_usd=2.0)
    refused = ledger.admit(kid, T0, spend(2.0, b=2.0))  # purpose b is at its cap, whatever the parent has left
    assert refused.kind == "refuse" and refused.stop_reason == "refused:purpose:b"
    ok = ledger.admit(child(p, usd=1.0, purpose="b", purpose_cap_usd=2.0), T0, spend(0.5, b=0.5))
    assert ok.kind == "admit" and "purpose:b" in ok.reservation.counters


def test_children_are_one_level_deep(tmp_path):
    ledger = make(tmp_path)
    p = parent_of(ledger, usd=3.0)
    kid = ledger.admit(child(p, usd=1.0), T0, spend())
    assert kid.kind == "admit"
    grandchild = ledger.admit(req(parent_run_id=kid.reservation.run_id, usd=0.5), T0, spend())
    assert grandchild.kind == "refuse" and grandchild.stop_reason == "refused:parent_is_child"
    # a child of the top run is still fine
    assert ledger.admit(child(p, usd=1.0), T0, spend()).kind == "admit"


def test_a_run_that_reports_no_cost_counts_at_the_default_estimate_inside_the_parent(tmp_path):
    assert queue.Request(job="j", run_id="r").usd == runrecord.UNKNOWN_RUN_USD == 1.0
    cost = runrecord.record_cost({"status": "ok", "cost_usd": None, "tokens": runrecord.blank_tokens()})
    assert cost == runrecord.UNKNOWN_RUN_USD
    ledger = make(tmp_path)
    p = parent_of(ledger, usd=2.5)
    first = ledger.admit(
        queue.Request(job="kid", run_id="20261108T030000Z-aaaaaa", parent_run_id=p.run_id, turns=1, seconds=1.0),
        T0,
        spend(),
    )
    assert first.reservation.usd == 1.0
    ledger.settle(first.reservation, status="ok", usd=cost, tokens=None, stop_reason="completed", now=T0)
    second = ledger.admit(
        queue.Request(
            job="kid2", run_id="20261108T030000Z-bbbbbb", parent_run_id=p.run_id, turns=1, seconds=1.0, usd=2.0
        ),
        T0,
        spend(1.0),
    )
    assert second.reservation.usd == pytest.approx(1.5) and second.reservation.resized  # 2.5 - 1.0 spent


def test_a_store_from_before_the_parent_column_gains_it_and_keeps_its_receipts(tmp_path):
    folder = tmp_path / "state" / "queue"
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / "queue.db")
    db.executescript(queue._SCHEMA.replace("    parent_run_id TEXT,\n", ""))
    db.execute(
        "INSERT INTO receipts (run_id, job, status, month, queued_at, started_at, reserved_usd) "
        "VALUES ('old1', 'j', 'running', '2026-11', 1, 1, 2.0)"
    )
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    ledger = make(tmp_path, probe=lambda pid: (True, "100"))
    cols = {r[1] for r in ledger.conn().execute("PRAGMA table_info(receipts)")}
    assert (
        "parent_run_id" in cols and ledger.conn().execute("PRAGMA user_version").fetchone()[0] == queue.SCHEMA_VERSION
    )
    assert ledger.receipt("old1")["parent_run_id"] is None
    ledger.close()
    assert (
        make(tmp_path, probe=lambda pid: (True, "100")).receipt("old1")["reserved_usd"] == 2.0
    )  # opening twice is fine


# ---- naming the parent ------------------------------------------------------------------------------------------------


def test_parent_run_is_a_job_key_that_names_a_run(tmp_path):
    base = 'runner = "claude-code"\nmodel = "m"\nprompt = "p"\n'
    j = jobs.loads("j", base + 'parent_run = "20261108T030000Z-0a1b2c"\n', tmp_path)
    assert j.parent_run == "20261108T030000Z-0a1b2c" and jobs.loads("j", base, tmp_path).parent_run is None
    for bad in ('"a b"', '"../x"', "3"):
        with pytest.raises(jobs.JobError, match="parent_run"):
            jobs.loads("j", base + f"parent_run = {bad}\n", tmp_path)
    r = queue.request_for(j, "rid", cap_usd=None, unknown_run_usd=None, purpose_cap_usd=None, limits=queue.Limits())
    assert r.parent_run_id == "20261108T030000Z-0a1b2c"


def test_ws_run_parent_spends_from_a_run_in_progress_and_refuses_without_one(home, capsys):
    write_job(home, "kid", extra="budget_usd = 1.5")
    holder = queue.ledger_for_env()
    parent = holder.admit(req(job="parent", usd=4.0, cap_usd=20.0), datetime.now(UTC), spend())
    assert parent.kind == "admit"
    assert cli.main(["run", "kid", "--parent", parent.reservation.run_id]) == 0
    (rec,) = runrecord.read(runrecord.runs_path())
    row = holder.receipt(rec["run_id"])
    assert row["parent_run_id"] == parent.reservation.run_id and "parent_budget" in rec["reservation"]["counters"]
    assert cli.main(["run", "kid", "--parent", "20261108T000000Z-ffffff"]) == 3
    assert runrecord.read(runrecord.runs_path())[-1]["stop_reason"] == "refused:parent_not_running"
    assert cli.main(["run", "kid", "--parent", "not a run id"]) == 2
    assert len(saw(home)) == 1  # only the first one ran
    capsys.readouterr()
    assert os.environ.get("WS_HOME")
