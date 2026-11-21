"""Slot fairness and the weekly report: arrival order, the maximum wait requeues, a session's share, three toil counts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_queue import T0, Clock, close, make, req, spend

from ws import cli, jsonl, queue


def slot(session=None, who="writer", lane="light", **kw) -> queue.Request:
    return req(lane=lane, who=who, session=session, usd=0.0, turns=0, seconds=0.0, cap_usd=None, **kw)


def held(ledger, session) -> int:
    rows = (
        ledger.conn()
        .execute("SELECT COUNT(*) FROM receipts WHERE status = 'running' AND session IS ?", (session,))
        .fetchone()
    )
    return rows[0]


def test_two_waiting_sessions_are_granted_in_arrival_order(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5))
    holder = slot("s0", lane="ui")
    assert ledger.admit(holder, clock(), spend()).kind == "admit"
    first, second = slot("s1", lane="ui"), slot("s2", lane="ui")
    assert ledger.admit(first, clock(), spend()).kind == "wait"
    clock.advance(seconds=1)
    assert ledger.admit(second, clock(), spend()).kind == "wait"
    close(ledger, holder.run_id, clock())
    assert ledger.admit(second, clock(), spend()).counter == "queue_ahead"  # the later one waits its turn
    assert ledger.admit(first, clock(), spend()).kind == "admit"


def test_a_waiter_past_the_maximum_wait_is_requeued_not_failed(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5))
    holder = ledger.admit(slot("s0", lane="ui"), clock(), spend()).reservation
    waiter, later = slot("s1", lane="ui"), slot("s2", lane="ui")
    told: list[float] = []
    looks = []

    def sleep(s):
        clock.advance(seconds=s)
        looks.append(s)
        if len(looks) == 1:
            assert ledger.admit(later, clock(), spend()).kind == "wait"  # a later arrival joins the line
        if len(looks) == 30:  # 150 s: past the 60 s maximum twice
            ledger.settle(holder, status="ok", usd=0, tokens=None, stop_reason="completed", now=clock())

    got = queue.wait_for_slot(ledger, waiter, max_wait_s=60, sleep=sleep, on_requeue=told.append)
    assert got.run_id == waiter.run_id  # granted ahead of the later arrival: the requeue kept its place
    assert len(told) == 2 and told[0] >= 60
    assert ledger.receipt(waiter.run_id)["requeues"] == 2
    assert ledger.receipt(waiter.run_id)["status"] == "running"


def test_the_timeout_is_still_the_callers_hard_stop(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5))
    ledger.admit(slot("s0", lane="ui"), clock(), spend())
    with pytest.raises(queue.SlotTimeout):
        queue.wait_for_slot(
            ledger, slot("s1", lane="ui"), timeout_s=100, max_wait_s=30, sleep=lambda s: clock.advance(seconds=s)
        )


def test_one_session_cannot_hold_every_light_slot_while_another_waits(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(max_concurrent=4, poll_s=5))
    a = [slot("A") for _ in range(4)]
    b = [slot("B") for _ in range(4)]
    assert ledger.admit(a[0], clock(), spend()).kind == "admit"
    assert ledger.admit(a[1], clock(), spend()).kind == "admit"
    clock.advance(seconds=1)
    assert ledger.admit(b[0], clock(), spend()).kind == "admit"  # two sessions: a share is 2 of 4
    for r in b[1:]:
        ledger.admit(r, clock(), spend())
    for r in a[2:]:
        assert ledger.admit(r, clock(), spend()).kind == "wait"
    assert held(ledger, "A") == 2 and held(ledger, "B") == 2
    for r in a[2:] + b[2:]:  # every waiter looks again: neither session goes past its share while the other waits
        ledger.admit(r, clock(), spend())
    assert held(ledger, "A") <= 2 and held(ledger, "B") <= 2


def test_a_session_alone_gets_every_slot(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(max_concurrent=4, poll_s=5))
    assert [ledger.admit(slot("A"), clock(), spend()).kind for _ in range(4)] == ["admit"] * 4
    assert ledger.admit(slot("A"), clock(), spend()).counter == "concurrency"


def test_a_share_held_back_waiter_does_not_hold_back_another_session(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(max_concurrent=4, poll_s=5))
    a = [slot("A") for _ in range(3)]
    b = [slot("B") for _ in range(3)]
    for r in a[:2] + b[:2]:
        assert ledger.admit(r, clock(), spend()).kind == "admit"  # the lane is full, 2 and 2
    assert ledger.admit(a[2], clock(), spend()).kind == "wait"  # A's third arrives first...
    clock.advance(seconds=1)
    assert ledger.admit(b[2], clock(), spend()).kind == "wait"  # ...then B's
    close(ledger, b[0].run_id, clock())  # B drops to 1: A is at its share while B waits
    assert ledger.admit(a[2], clock(), spend()).counter == "session_share"
    assert ledger.admit(b[2], clock(), spend()).kind == "admit"  # B is not held behind A's earlier, held-back waiter


def test_slot_session_comes_from_the_environment():
    assert queue.slot_session({"WS_SLOT_SESSION": "x", "CLAUDE_CODE_SESSION_ID": "y"}) == "x"
    assert queue.slot_session({"CLAUDE_CODE_SESSION_ID": "y"}) == "y"
    assert queue.slot_session({}) is None


def test_every_lane_keeps_the_three_times(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    run_id = ledger.enqueue("j", None, clock())[0]
    r = req(job="j", run_id=run_id)
    clock.advance(seconds=30)
    assert ledger.admit(r, clock(), spend()).kind == "admit"
    clock.advance(seconds=60)
    close(ledger, run_id, clock())
    got = ledger.receipt(run_id)
    assert got["queued_at"] and got["started_at"] and got["finished_at"]
    assert got["queued_at"] < got["started_at"] < got["finished_at"]


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A state folder with a week of gate lines, asks and receipts: 3 denies, 2 hand-offs, 1 stall, 2 cap kills."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    now = datetime.now(UTC)
    day = now - timedelta(days=2)
    from ws import asks, paths

    logs = paths.state_dir() / "logs"
    for decision in ("deny", "deny", "allow", "ask", "deny"):
        jsonl.append(logs / "gate.jsonl", jsonl.stamped({"decision": decision}, 1, day.timestamp()))
    jsonl.append(logs / "gate.jsonl", jsonl.stamped({"decision": "deny"}, 1, (now - timedelta(days=9)).timestamp()))
    store = asks.store_path()
    ms = int(day.timestamp() * 1000)
    for event in ("added", "answered", "added"):
        jsonl.append(
            store, {"schema": 1, "ts": ms, "at": jsonl.iso_utc(day.timestamp()), "event": event}, time_keys=("at",)
        )
    ledger = queue.ledger_for_env()
    ledger.clock = lambda: day
    stalled = slot("A", lane="ui")
    ledger.admit(slot("Z", lane="ui"), day, spend())
    ledger.admit(stalled, day, spend())
    ledger.requeue(stalled.run_id, 61, day)
    for stop in ("timeout", "max_turns", "completed"):
        r = req(job=f"cap-{stop}")
        res = ledger.admit(r, day, spend()).reservation
        ledger.settle(res, status="error", usd=0, tokens=None, stop_reason=stop, now=day + timedelta(minutes=1))
    ledger.close()
    return now


def test_the_weekly_report_prints_one_row_with_the_three_counts(planted, capsys):
    assert cli.main(["--json", "queue", "report", "--weekly"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["data"]
    assert (row["blocks"], row["stalls"], row["handoffs"]) == (3, 1, 2)
    assert (row["runs"], row["killed"]) == (3, 2)
    assert cli.main(["queue", "report"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and "blocks" in lines[0] and lines[1].split()[1:4] == ["3", "1", "2"]


def test_the_report_goes_back_by_weeks(planted, capsys):
    assert cli.main(["--json", "queue", "report", "--weeks", "2"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert [r["blocks"] for r in rows] == [3, 1]


def test_an_old_store_gains_the_session_columns(tmp_path):
    ledger = make(tmp_path)
    old = ledger.enqueue("j", None, T0)[0]
    c = ledger.conn()
    c.execute("ALTER TABLE receipts DROP COLUMN session")
    c.execute("ALTER TABLE receipts DROP COLUMN requeues")
    c.execute("PRAGMA user_version = 5")
    ledger.close()
    again = make(tmp_path)
    cols = {r[1] for r in again.conn().execute("PRAGMA table_info(receipts)")}
    assert {"session", "requeues"} <= cols and again.receipt(old)["requeues"] == 0
