"""The run record: its fields, the two rules `append` keeps, and the outcomes a job can come to.

Every record here is built at run time in a scratch folder; nothing reads the real HOME or the clock's timezone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ws import canary, jsonl, runrecord

T0 = datetime(2026, 11, 8, 3, 0, 0, tzinfo=UTC)
RID = "20261108T030000Z-abcdef"


def log_in(tmp_path):
    path = tmp_path / "logs" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def rec(**kw) -> dict:
    base = {
        "run_id": RID,
        "job": "j",
        "runner": "claude-code",
        "started_at": runrecord.iso(T0),
        "ended_at": runrecord.iso(T0),
        "ws_version": "0",
    }
    base.update(kw)
    return runrecord.new(**base)


def test_every_new_field_is_present_and_null_until_set():
    r = runrecord.new()
    for key in ("stop_subtype", "fault", "retry_after_s", "approval", "run_key", "chain_depth"):
        assert key in r and r[key] is None
    assert set(runrecord.schema()["required"]) == set(runrecord.KEYS)
    assert set(runrecord.schema()["properties"]) == set(runrecord.KEYS)
    with pytest.raises(KeyError):
        runrecord.new(not_a_field=1)


@pytest.mark.parametrize(
    ("subtype", "reason"),
    [
        ("error_max_turns", "max_turns"),
        ("error_max_budget_usd", "max_budget"),
        ("error_during_execution", "runner_error"),
    ],
)
def test_a_turn_or_budget_stop_is_never_ok(subtype, reason):
    r = runrecord.checked(rec(status="ok", stop_reason="completed", stop_subtype=subtype))
    assert (r["status"], r["stop_reason"], r["stop_subtype"]) == ("error", reason, subtype)
    assert r["error"]


def test_a_success_subtype_stays_ok_and_an_error_gains_the_limit_as_its_reason():
    assert runrecord.checked(rec(status="ok", stop_reason="completed", stop_subtype="success"))["status"] == "ok"
    assert runrecord.checked(rec(status="ok", stop_reason="completed"))["status"] == "ok"
    r = runrecord.checked(rec(status="error", stop_reason="runner_error", stop_subtype="error_max_turns"))
    assert (r["status"], r["stop_reason"]) == ("error", "max_turns")
    r = runrecord.checked(rec(status="timeout", stop_reason="timeout", stop_subtype="error_max_turns"))
    assert r["stop_reason"] == "timeout"  # a reason ws already knew is kept


def test_append_is_the_door_that_keeps_the_rules(tmp_path):
    log = log_in(tmp_path)
    runrecord.append(log, rec(status="ok", stop_reason="completed", stop_subtype="error_max_turns", fault="provider"))
    (got,) = runrecord.read(log)
    assert (got["status"], got["stop_reason"], got["stop_subtype"]) == ("error", "max_turns", "error_max_turns")
    log2 = log_in(tmp_path / "b")
    runrecord.append(log2, rec(status="ok", stop_reason="completed", fault="provider"))
    assert runrecord.read(log2)[0]["fault"] is None  # an ok run names no fault


@pytest.mark.parametrize("kind", runrecord.UPSTREAM)
def test_an_upstream_failure_is_the_providers_fault(kind):
    fields = runrecord.upstream(kind, retry_after_s=30 if kind == "rate_limited" else None)
    r = runrecord.checked(rec(**fields))
    assert (r["status"], r["stop_reason"], r["fault"]) == ("error", f"upstream:{kind}", "provider")
    assert r["retry_after_s"] == (30.0 if kind == "rate_limited" else None)
    assert canary.validate(json.loads(json.dumps(r)), runrecord.schema()) == []


def test_upstream_and_expected_take_only_their_typed_kinds():
    with pytest.raises(ValueError):
        runrecord.upstream("the server said 503")
    with pytest.raises(ValueError):
        runrecord.expected("error")
    assert runrecord.upstream("rate_limited", retry_after_s=float("nan"))["retry_after_s"] is None
    assert runrecord.upstream("unavailable", retry_after_s=-1)["retry_after_s"] is None


@pytest.mark.parametrize("kind", runrecord.EXPECTED)
def test_skip_and_nothing_to_do_are_expected_outcomes(kind):
    r = runrecord.checked(rec(**runrecord.expected(kind)))
    assert (r["status"], r["stop_reason"], r["fault"]) == ("ok", kind, None)


def test_an_unknown_outcome_has_its_real_duration_and_counts_as_started():
    r = runrecord.unknown_outcome(RID, "j", T0, T0 + timedelta(seconds=42.5), purpose="p", ws_version="0")
    assert (r["status"], r["stop_reason"], r["duration_ms"], r["purpose"]) == (
        "outcome_unknown",
        "owner_gone",
        42500,
        "p",
    )
    assert r["ended_at"] == "2026-11-08T03:00:42.500Z" and r["exit_code"] is None
    assert runrecord.unknown_outcome(RID, "j", T0, T0 - timedelta(seconds=5))["duration_ms"] == 0  # a clock step back
    assert runrecord.record_cost(r, 2.0, None) == 2.0  # it started: it counts at the fallback, never as free
    assert canary.validate(json.loads(json.dumps(r)), runrecord.schema()) == []


def test_the_approval_run_key_and_chain_depth_validate():
    r = rec(
        status="ok", stop_reason="completed",
        approval={"switch": "act-and-tell", "quote": "yes, run the backup tonight"}, run_key="j:msg-1", chain_depth=1,
    )  # fmt: skip
    assert canary.validate(json.loads(json.dumps(r)), runrecord.schema()) == []
    bad = dict(r, approval={"switch": "act"})
    assert canary.validate(json.loads(json.dumps(bad)), runrecord.schema()) != []


def test_a_record_written_before_these_fields_still_reads_and_counts(tmp_path):
    """Rollback the other way: the reap record today's main writes (error, crashed, duration 0, no new keys)."""
    old = {k: None for k in runrecord.KEYS[: runrecord.KEYS.index("reservation") + 1]}
    old.update(
        schema=3, run_id=RID, job="j", started_at="2026-11-08T03:00:00.000Z", ended_at="2026-11-08T03:00:00.000Z",
        status="error", stop_reason="crashed", duration_ms=0, models_used=[], secrets_used=[], recovered=[],
        tokens=runrecord.blank_tokens(), error="interrupted: the process that held this run was gone before it finished",
    )  # fmt: skip
    log = log_in(tmp_path)
    log.write_text(json.dumps(old) + "\n", encoding="utf-8")
    (got,) = runrecord.read(log)
    assert got.get("stop_subtype") is None and got.get("chain_depth") is None
    assert runrecord.month_spend([got], T0, 2.0) == 2.0  # counted as a started run, as it was


def test_a_new_record_reads_through_the_plain_line_reader(tmp_path):
    """The reader every ws version shares (`jsonl.read`) takes a record with the new status and keys as it is."""
    log = log_in(tmp_path)
    runrecord.append(log, runrecord.unknown_outcome(RID, "j", T0, T0 + timedelta(seconds=3)))
    (got,) = jsonl.read(log)
    assert got["status"] == "outcome_unknown" and got["duration_ms"] == 3000


# The cost rules of ws before `outcome_unknown` (main at e9dcf61), frozen here to measure what a rollback costs.
def _old_started(rec: dict) -> bool:
    if rec.get("status") in ("ok", "timeout", "preempted"):
        return True
    return rec.get("status") == "error" and (
        rec.get("exit_code") is not None or str(rec.get("error", "")).startswith("interrupted")
    )


def _old_record_cost(rec: dict, unknown_run_usd: float | None = None) -> float:
    if runrecord._finite(rec.get("cost_usd")):
        return float(rec["cost_usd"])
    if not _old_started(rec):
        return 0.0
    return float(unknown_run_usd or runrecord.UNKNOWN_RUN_USD)


def test_a_reaped_record_under_the_reader_before_outcome_unknown():
    """Known rollback cost (docs/reference/stores.md): the old rules read the record but count it at US$0; these rules
    count it at the fallback. The old reap shape counts the same under both."""
    new = runrecord.unknown_outcome(RID, "j", T0, T0 + timedelta(seconds=9), ws_version="0")
    assert _old_record_cost(new, 2.0) == 0.0 and runrecord.record_cost(new, 2.0) == 2.0
    old = dict(new, status="error", stop_reason="crashed", error="interrupted: the process ... was gone")
    assert _old_record_cost(old, 2.0) == runrecord.record_cost(old, 2.0) == 2.0


def test_checked_leaves_the_callers_record_alone():
    mine = rec(status="ok", stop_subtype="error_max_turns")
    assert runrecord.checked(mine)["status"] == "error" and mine["status"] == "ok"


def test_a_lone_outcome_unknown_record_is_read_and_a_real_one_wins_over_it(tmp_path):
    log = log_in(tmp_path)
    runrecord.append(log, runrecord.unknown_outcome(RID, "j", T0, T0 + timedelta(seconds=1), ws_version="0"))
    other = "20261108T030001Z-000001"
    runrecord.append(log, runrecord.unknown_outcome(other, "j", T0, T0, ws_version="0"))
    runrecord.append(log, rec(run_id=other, status="ok", stop_reason="completed", cost_usd=0.5))
    got = {(r["run_id"], r["status"]) for r in runrecord.read(log)}
    assert got == {(RID, "outcome_unknown"), (other, "ok")}
