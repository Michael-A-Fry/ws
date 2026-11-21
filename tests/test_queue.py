"""`ws queue`: reserve before a run, settle from what it used, wait for a slot, and the engine underneath.

The ledger is driven with an injected clock, memory reader and process table, so no test needs real time, real memory or
a real process. The commands run against a scratch WS_HOME with a small fake `claude` on PATH and the broker faked at its
client functions (tests/test_run.py does the same for `ws run`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ws import broker, cli, jobs, output, paths, policy, queue, run, runrecord, usage
from ws._vendor import sqlqueue
from ws.adapters import claude_code
from ws.profile import dumps
from ws.roles import launchd, systemd

CONTRACT = (
    'timeout_s = 600\non_failure = "alert"\nlog = "file"\nuninstall = "ws schedule remove x"\n'
    'expected_rate = "1d"\ndata_class = "none"\n'
)

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 8, 3, 0, 0, tzinfo=UTC)
URL = "http://127.0.0.1:1/l/" + "T" * 43

FAKE = f"""#!{sys.executable}
import json, os, signal, sys
if "--version" in sys.argv:
    print("9.9.9 (Fake Code)")
    sys.exit(0)
prompt = sys.stdin.read()
with open(os.path.join(os.environ["WS_HOME"], "fake-saw.jsonl"), "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": prompt}}) + "\\n")
ok = open({str(FIXTURES / "claude-result-ok.json")!r}).read()
if prompt == "ok":
    sys.stdout.write(ok)
if prompt == "nocost":
    r = json.loads(ok)
    r.pop("total_cost_usd")
    r["modelUsage"] = {{}}
    sys.stdout.write(json.dumps(r))
if prompt == "term":
    os.kill(os.getpid(), signal.SIGTERM)
if prompt == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
if prompt.startswith("chain:"):
    # what a script or watcher inside a queued run does to queue the next link: the ledger (the CLI add refuses in
    # any agent session)
    import subprocess
    env = dict(os.environ)
    if prompt == "chain:reset":
        env["WS_CHAIN_DEPTH"] = "0"  # a run that tries to start its chain again
    child = chr(10).join([
        "import sys",
        "from datetime import UTC, datetime",
        "from ws import queue",
        "try:",
        "    queue.ledger_for_env().enqueue('chainer', None, datetime.now(UTC),",
        "        chain_depth=queue.chain_depth_from_env(), chain_parent=queue.chain_parent_from_env())",
        "except queue.WsError as e:",
        "    sys.exit(e.exit_code)",
    ])
    r = subprocess.run([sys.executable, "-c", child], env=env)
    with open(os.path.join(os.environ["WS_HOME"], "chain-saw.jsonl"), "a") as f:
        saw = dict(depth=os.environ.get("WS_CHAIN_DEPTH"), parent=os.environ.get("WS_CHAIN_PARENT"), rc=r.returncode)
        f.write(json.dumps(saw) + chr(10))
    sys.stdout.write(ok)
"""


class Clock:
    """A clock a test moves."""

    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class Host:
    """A fake host: memory the test sets, and a process table."""

    def __init__(self, avail=8.0, total=16.0):
        self.avail, self.total = avail, total
        self.procs: dict[int, tuple[bool | None, str | None]] = {}

    def memory(self):
        return (self.avail, self.total)

    def probe(self, pid):
        return self.procs.get(pid, (True, "100"))


_ids = iter(range(1, 10**6))


def make(tmp_path, clock=None, host=None, **kw) -> queue.Ledger:
    limits = kw.pop("limits", queue.Limits(busy_s=0.2))
    host = host or Host()
    return queue.Ledger(
        tmp_path / "state",
        limits=limits,
        memory=kw.pop("memory", host.memory),
        probe=kw.pop("probe", host.probe),
        rate_limits=kw.pop("rate_limits", lambda now: None),
        clock=clock or Clock(),
        **kw,
    )


def req(**kw) -> queue.Request:
    n = next(_ids)
    base = {
        "job": f"job{n}",
        "run_id": f"20261108T030000Z-{n:06x}",
        "cap_usd": 100.0,
        "usd": 1.0,
        "turns": 5,
        "seconds": 60.0,
    }
    base.update(kw)
    return queue.Request(**base)


def running(ledger) -> list[dict]:
    return [r for r in ledger.receipts(last=100, active_only=True) if r["status"] == "running"]


def spend(total=0.0, **by) -> callable:
    return lambda: queue.Spend(total, by)


# ---- the engine -----------------------------------------------------------------------------------------------------


def test_engine_claim_ids_fence_a_stale_holder(tmp_path):
    t = [1_000_000_000]
    q = sqlqueue.SqlQueue(tmp_path / "q.db", clock=lambda: t[0])
    q.put("a")
    first = q.pop()
    assert first.status == sqlqueue.Status.LOCKED and first.claim_id == 1
    t[0] += 120 * sqlqueue.NS
    assert q.retry_expired(60) == 1  # the lease ran out
    second = q.pop()
    assert second.message_id == first.message_id and second.claim_id == 2
    with pytest.raises(sqlqueue.StaleClaim):
        q.done(first.message_id, first.claim_id)  # the old holder comes back late
    assert q.get(first.message_id).status == sqlqueue.Status.LOCKED
    with pytest.raises(sqlqueue.StaleClaim):
        q.done(first.message_id, 99)  # a made-up id
    assert q.done(second.message_id, second.claim_id).status == sqlqueue.Status.DONE
    with pytest.raises(sqlqueue.StaleClaim):
        q.mark_failed(second.message_id, second.claim_id)  # already finished: nothing changes
    assert q.get(first.message_id).status == sqlqueue.Status.DONE


def test_engine_idempotency_keys_maxsize_retry_and_list_locked(tmp_path):
    t = [10 * sqlqueue.NS]
    q = sqlqueue.SqlQueue(tmp_path / "q.db", maxsize=2, clock=lambda: t[0])
    a = q.put("one", idempotency_key="k")
    assert q.put("one", idempotency_key="k").message_id == a.message_id  # a replay adds nothing
    with pytest.raises(sqlqueue.IdempotencyConflict):
        q.put("other", idempotency_key="k")
    q.put("two")
    with pytest.raises(sqlqueue.QueueFull):
        q.put("three")
    m = q.pop()
    assert [x.message_id for x in q.list_locked()] == [m.message_id]
    assert q.list_locked(older_than_s=30) == []
    t[0] += 31 * sqlqueue.NS
    assert len(q.list_locked(older_than_s=30)) == 1
    back = q.retry(m.message_id, m.claim_id, delay_s=60)
    assert back.status == sqlqueue.Status.READY and back.tries == 1
    assert q.pop().data == "two"  # "one" is not ready for another minute
    assert q.pop() is None
    t[0] += 61 * sqlqueue.NS
    assert q.pop().data == "one"


def test_engine_shares_a_callers_connection_and_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    q = sqlqueue.SqlQueue(conn, table="inbox")
    conn.execute("CREATE TABLE other (v)")
    with pytest.raises(RuntimeError), q.transaction():
        conn.execute("INSERT INTO other VALUES (1)")
        q.put("m")
        raise RuntimeError
    assert conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 0 and q.empty()
    with q.transaction():
        conn.execute("INSERT INTO other VALUES (1)")
        q.put("m")
    assert q.qsize() == 1 and conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 1


def test_engine_names_a_store_error_by_its_code_not_its_text():
    def err(code, text):
        e = sqlite3.OperationalError(text)
        e.sqlite_errorcode = code
        return e

    assert sqlqueue.store_error_kind(err(5, "database is full")) == "locked"  # the text lies; the code decides
    assert sqlqueue.store_error_kind(err(6 | (1 << 8), "x")) == "locked"  # an extended code keeps its primary
    assert sqlqueue.store_error_kind(err(13, "database is locked")) == "full"
    assert sqlqueue.store_error_kind(err(8, "x")) == "readonly"
    assert sqlqueue.store_error_kind(err(11, "x")) == "unreadable"
    assert sqlqueue.store_error_kind(sqlite3.OperationalError("database is locked")) == "other"


def test_the_engine_file_carries_no_trace_of_where_it_came_from():
    name = "lite" + "queue"
    for i, line in enumerate((Path(queue.__file__).parent / "_vendor" / "sqlqueue.py").read_text().splitlines(), 1):
        if line.startswith("# Copyright"):
            continue
        assert name not in line.lower() and "http" not in line.lower(), f"line {i}"


# ---- admission --------------------------------------------------------------------------------------------------------


def test_reserves_all_counters_or_none(tmp_path):
    host = Host()
    ledger = make(tmp_path, host=host)
    first = ledger.admit(req(purpose="jobs", purpose_cap_usd=5.0, usd=2.0, turns=10, seconds=60, ram_gb=1), T0, spend())
    assert first.kind == "admit"
    assert set(first.reservation.counters) >= {"profile_cap", "purpose:jobs", "turns", "time", "concurrency", "memory"}
    assert [r["reserved_usd"] for r in running(ledger)] == [2.0]
    # one counter short (the purpose has 4.5 spent and 2.0 reserved of 5): nothing is reserved, though the rest have room
    short = ledger.admit(req(purpose="jobs", purpose_cap_usd=5.0, usd=1.0), T0, spend(4.5, jobs=4.5))
    assert short.kind == "refuse" and short.counter == "purpose:jobs"
    # another is only full for now (memory): a wait, and nothing is reserved either
    host.avail = 2.0
    waits = ledger.admit(req(usd=1.0, ram_gb=1), T0, spend())
    assert waits.kind == "wait" and waits.counter == "memory"
    assert [r["reserved_usd"] for r in running(ledger)] == [2.0]
    assert sum(r["reserved_turns"] for r in running(ledger)) == 10


def test_resizes_to_what_is_left_and_passes_it_as_max_budget(tmp_path):
    ledger = make(tmp_path)
    d = ledger.admit(req(cap_usd=10.0, usd=1.0), T0, spend(9.7))
    assert d.kind == "admit" and d.reservation.resized and d.reservation.usd == pytest.approx(0.3)
    assert d.reservation.max_budget_usd == pytest.approx(0.3)
    job = jobs.Job(name="j", runner="claude-code", model="m", prompt="p")
    job = dataclasses.replace(job, max_budget_usd=d.reservation.max_budget_usd)
    argv, _ = claude_code.build_command(job, Path("/x"), None)
    assert argv[argv.index("--max-budget-usd") + 1] == "0.3000"
    argv, _ = claude_code.build_command(dataclasses.replace(job, max_budget_usd=None), Path("/x"), None)
    assert "--max-budget-usd" not in argv
    # a job that says strict is never run on less than its estimate
    strict = ledger.admit(req(cap_usd=10.0, usd=1.0, strict=True), T0, spend(9.7))
    assert strict.kind == "refuse" and strict.counter == "profile_cap" and "strict" in strict.reason
    # an estimate the job named is the limit the runner gets, resized or not; a default estimate is only a guess
    named = ledger.admit(req(cap_usd=100.0, usd=1.5, explicit_usd=True), T0, spend())
    guess = ledger.admit(req(cap_usd=100.0, usd=1.5), T0, spend())
    assert named.reservation.max_budget_usd == 1.5 and not named.reservation.resized
    assert guess.reservation.max_budget_usd is None
    # room at or below nothing is a refusal, not a zero reservation
    gone = ledger.admit(req(cap_usd=10.0), T0, spend(10.0))
    assert gone.kind == "refuse" and gone.counter == "profile_cap" and gone.stop_reason == "refused:profile_cap"


def test_each_purpose_refused_at_its_own_cap_with_a_typed_reason(tmp_path):
    ledger = make(tmp_path)
    caps = {"jobs": 40.0, "evals": 60.0}
    by = {"jobs": 40.0, "evals": 10.0}

    def ask(purpose):
        return ledger.admit(req(purpose=purpose, purpose_cap_usd=caps[purpose], cap_usd=100.0), T0, spend(50.0, **by))

    jobs_d, evals_d = ask("jobs"), ask("evals")
    assert jobs_d.kind == "refuse" and jobs_d.counter == "purpose:jobs"
    assert (
        jobs_d.stop_reason == "refused:purpose:jobs"
        and "purpose jobs" in jobs_d.reason
        and "[budget.purposes]" in jobs_d.hint
    )
    assert evals_d.kind == "admit"  # the profile has room and so does this purpose
    by["evals"] = 60.0
    assert ask("evals").counter == "purpose:evals"
    assert [r["status"] for r in ledger.receipts(last=10) if r["purpose"]].count("refused") == 2
    # the profile's cap says no whatever a purpose has
    assert (
        ledger.admit(req(purpose="evals", purpose_cap_usd=60.0, cap_usd=50.0), T0, spend(50.0)).counter == "profile_cap"
    )


def test_race_for_last_slot_admits_one(tmp_path):
    gate = threading.Barrier(2, timeout=20)
    out: list[queue.Decision] = []
    limits = queue.Limits(max_concurrent=1, busy_s=10)

    def go(n):
        ledger = make(tmp_path, limits=limits)  # each thread has its own connection, as two processes would
        r = req(job=f"racer{n}")
        gate.wait()
        out.append(ledger.admit(r, T0, spend()))

    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(d.kind for d in out) == ["admit", "wait"]
    assert next(d for d in out if d.kind == "wait").counter == "concurrency"


def test_race_for_the_last_dollar_admits_one(tmp_path):
    gate = threading.Barrier(2, timeout=20)
    out: list[queue.Decision] = []

    def go(n):
        ledger = make(tmp_path, limits=queue.Limits(busy_s=10))
        r = req(job=f"racer{n}", cap_usd=1.0, usd=1.0, strict=True)
        gate.wait()
        out.append(ledger.admit(r, T0, spend()))

    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(d.kind for d in out) == ["admit", "refuse"]
    assert next(d for d in out if d.kind == "refuse").counter == "profile_cap"


def test_settle_is_idempotent(tmp_path):
    ledger = make(tmp_path)
    res = ledger.admit(req(usd=2.0), T0, spend()).reservation
    assert ledger.settle(res, status="ok", usd=1.25, tokens={"input": 3}, stop_reason="completed", now=T0)
    assert not ledger.settle(res, status="error", usd=9.0, tokens=None, stop_reason="x", now=T0)  # changes nothing
    (row,) = ledger.receipts(last=5)
    assert row["status"] == "ok" and row["actual_usd"] == 1.25 and row["stop_reason"] == "completed" and row["settled"]
    assert row["tokens"] == {"input": 3}
    # only the claim that reserved can settle: a forged or stale claim is refused, and so is a run that is not there
    mine = ledger.admit(req(usd=1.0), T0, spend()).reservation
    with pytest.raises(sqlqueue.StaleClaim):
        ledger.settle(
            dataclasses.replace(mine, claim_id=mine.claim_id + 2),
            status="ok",
            usd=0,
            tokens=None,
            stop_reason=None,
            now=T0,
        )
    with pytest.raises(sqlqueue.StaleClaim):
        ledger.settle(
            dataclasses.replace(mine, run_id="nope"), status="ok", usd=0, tokens=None, stop_reason=None, now=T0
        )
    assert len(running(ledger)) == 1  # still reserved: neither attempt freed it


def test_settled_spend_is_not_counted_twice(tmp_path):
    ledger = make(tmp_path)
    held = ledger.admit(req(cap_usd=10.0, usd=3.0, explicit_usd=True), T0, spend()).reservation
    probe = lambda: ledger.admit(req(cap_usd=10.0, usd=8.0), T0, spend(0.0), dry=True).reservation.usd
    assert probe() == pytest.approx(7.0)  # 10 less the 3 held
    # the run ends: its record (2.5) is now in the log, and the reservation is closed, so only the record counts
    assert ledger.settle(held, status="ok", usd=2.5, tokens=None, stop_reason="completed", now=T0)
    assert ledger.admit(req(cap_usd=10.0, usd=8.0), T0, spend(2.5), dry=True).reservation.usd == pytest.approx(7.5)
    # a run whose record could not be written is counted from its receipt, until a reaper writes the record
    lost = ledger.admit(req(cap_usd=10.0, usd=1.0, explicit_usd=True), T0, spend(2.5)).reservation
    ledger.settle(lost, status="ok", usd=2.0, tokens=None, stop_reason="completed", now=T0, record_written=False)
    assert ledger.admit(req(cap_usd=10.0, usd=8.0), T0, spend(2.5), dry=True).reservation.usd == pytest.approx(5.5)


def test_month_boundary_uses_one_utc_window(tmp_path):
    ledger = make(tmp_path)
    late = datetime(2026, 10, 31, 23, 59, 59, 900_000, tzinfo=UTC)
    midnight = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    held = ledger.admit(req(cap_usd=1.0, usd=1.0), late, spend()).reservation
    assert held.month == "2026-10"
    # a new month: October's open reservation is not November's, so the whole cap is there
    assert ledger.admit(req(cap_usd=1.0, usd=1.0), midnight, spend()).kind == "admit"
    # the window is UTC whatever zone the clock reading carries: 21:00 on 31 October in UTC is 10:00 on 1 November at +13
    nz = datetime(2026, 11, 1, 10, 0, tzinfo=timezone(timedelta(hours=13)))
    assert queue.month_of(nz) == "2026-10" == f"{policy.budget_month(nz)[0]:%Y-%m}"
    # the same text names the month in the run log, the purpose caps and the reservation
    recs = [
        runrecord.new(job="j", started_at="2026-10-31T23:59:59.900Z", status="ok", cost_usd=2.0, purpose="jobs"),
        runrecord.new(job="j", started_at="2026-11-01T00:00:00.000Z", status="ok", cost_usd=5.0),
    ]
    assert runrecord.month_spend(recs, late) == 2.0 and runrecord.month_spend(recs, midnight) == 5.0
    assert runrecord.month_spend_by_purpose(recs, late) == {"jobs": 2.0}
    for t in (late, midnight):
        assert sum(runrecord.month_spend_by_purpose(recs, t).values()) == runrecord.month_spend(recs, t)
    # a run that settles after the boundary still belongs to the month it was reserved in
    ledger.settle(held, status="ok", usd=0.4, tokens=None, stop_reason="completed", now=midnight)
    assert ledger.receipt(held.run_id)["month"] == "2026-10"


def test_unpriced_run_settles_at_the_same_fallback_the_cap_uses():
    rec = runrecord.new(
        job="j",
        started_at="2026-11-02T00:00:00.000Z",
        status="ok",
        exit_code=0,
        tokens={"input": 2_000_000, "output": 0, "cache_creation": 0, "cache_read": 0},
    )
    assert runrecord.record_cost(rec, 1.0, 3.0) == pytest.approx(6.0)
    assert runrecord.record_cost(rec, 1.0, None) == 1.0  # no price per token: the flat figure


def test_stale_owner_reaped_only_on_pid_and_start(tmp_path):
    host = Host()
    sink: list[dict] = []
    ledger = make(tmp_path, host=host, costing=(2.0, None), sink=lambda rec: sink.append(rec) or True)

    def hold(pid, start):
        host.procs[pid] = (True, start)
        r = req(owner_pid=pid)
        ledger.admit(r, T0, spend())
        return r.run_id

    alive, reused, dead, nostart = hold(4001, "A"), hold(4002, "B"), hold(4003, "C"), hold(4004, "D")
    host.procs[4002] = (True, "OTHER")  # the pid is running, but it is another process now
    host.procs[4003] = (False, None)  # the pid is gone
    host.procs[4004] = (None, None)  # this system cannot say; the lease decides
    assert ledger.reap(T0) == 2
    status = {r["run_id"]: r["status"] for r in ledger.receipts(last=10)}
    assert status[alive] == "running" and status[nostart] == "running"  # the lease has not run out
    assert status[reused] == "interrupted" and status[dead] == "interrupted"
    assert {r["stop_reason"] for r in sink} == {"owner_gone"} and len(sink) == 2
    assert all(r["status"] == "outcome_unknown" and r["duration_ms"] == 0 for r in sink)  # found gone at its start
    assert runrecord.record_cost(sink[0], 2.0, None) == 2.0  # counted at the fallback, like any run with no cost
    ledger.clock = lambda: T0 + timedelta(hours=2)
    assert ledger.reap(T0 + timedelta(hours=2)) == 1  # the unknown one, once its lease is over
    assert ledger.receipt(nostart)["status"] == "interrupted" and ledger.receipt(alive)["status"] == "running"
    assert ledger.reap(T0 + timedelta(hours=2)) == 0


def test_a_reaped_run_that_had_written_its_record_is_settled_from_it_not_recorded_twice(tmp_path):
    host = Host()
    sink: list[dict] = []
    r = req(owner_pid=4100)
    found = runrecord.new(
        run_id=r.run_id,
        job=r.job,
        started_at="2026-11-08T03:00:00.000Z",
        status="ok",
        cost_usd=0.5,
        stop_reason="completed",
    )
    ledger = make(
        tmp_path,
        host=host,
        sink=lambda rec: sink.append(rec) or True,
        recorded=lambda rid: found if rid == r.run_id else None,
    )
    ledger.admit(r, T0, spend())
    host.procs[4100] = (False, None)
    assert ledger.reap(T0) == 1 and sink == []
    row = ledger.receipt(r.run_id)
    assert row["status"] == "ok" and row["actual_usd"] == 0.5 and row["record_written"]


def test_store_locked_refuses_the_run(tmp_path):
    ledger = make(tmp_path, limits=queue.Limits(busy_s=0.05))
    ledger.admit(req(), T0, spend())  # the database exists now
    other = make(tmp_path, limits=queue.Limits(busy_s=0.05))
    holder = sqlite3.connect(ledger.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        d = other.admit(req(), T0, spend())
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert d.kind == "refuse" and d.counter == "store_locked" and d.stop_reason == "refused:store_locked"
    assert len(running(ledger)) == 1  # nothing was reserved by the refused call
    assert other.admit(req(), T0, spend()).kind == "admit"  # and the next call finds the store free


def test_a_store_that_cannot_be_opened_is_a_refusal(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue").write_bytes(b"a file where the queue folder should be")
    d = queue.Ledger(state, limits=queue.Limits(busy_s=0.05)).admit(req(), T0, spend())
    assert d.kind == "refuse" and d.counter.startswith("store_")
    bad = state / "queue2"
    bad.mkdir()
    (bad / "queue").mkdir()
    (bad / "queue" / "queue.db").write_bytes(b"this is not a database" * 100)
    d = queue.Ledger(bad, limits=queue.Limits(busy_s=0.05)).admit(req(), T0, spend())
    assert d.kind == "refuse" and d.counter == "store_unreadable"


def test_store_errors_are_classified_by_code(tmp_path):
    def err(code):
        e = sqlite3.OperationalError("x")
        e.sqlite_errorcode = code
        return e

    assert queue.store_error(err(5)).reason == "locked" and queue.store_error(err(13)).reason == "full"
    assert queue.store_error(err(8)).reason == "readonly"
    assert queue.store_error(OSError(28, "No space left on device")).reason == "full"
    assert queue.store_error(PermissionError(13, "denied")).reason == "readonly"
    assert queue.store_error(err(5)).exit_code == 3


def test_the_store_is_private_and_its_rules_are_in_the_database(tmp_path):
    ledger = make(tmp_path)
    ledger.admit(req(), T0, spend())
    assert stat.S_IMODE(ledger.folder.stat().st_mode) == 0o700 and stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    c = ledger.conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    ins = "INSERT INTO receipts (run_id, job, status, month, queued_at, finished_at) VALUES (?, ?, ?, '2026-11', 1, ?)"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("a", "x", "bogus", 1))  # a status that is not on the list
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("b", "x", "running", 1))  # running, yet finished
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("c", "x", "ok", None))  # finished status, yet no finish time
    held = req(job="same")
    assert ledger.admit(held, T0, spend()).kind == "admit"
    second = ledger.admit(req(job="same"), T0, spend())
    assert second.kind == "wait" and second.counter == "job_running"
    with pytest.raises(sqlite3.IntegrityError):  # and the index says it too, whatever the code checked
        c.execute(
            "INSERT INTO receipts (run_id, job, status, month, queued_at) VALUES ('d', 'same', 'running', '2026-11', 1)"
        )


def test_memory_reserve_is_kept(tmp_path):
    host = Host(avail=4.0, total=16.0)  # 15% of 16 is 2.4 GB that must stay free
    ledger = make(tmp_path, host=host)
    assert ledger.admit(req(ram_gb=1.5), T0, spend(), dry=True).kind == "admit"  # 4 - 1.5 = 2.5
    wait = ledger.admit(req(ram_gb=2.0), T0, spend(), dry=True)
    assert wait.kind == "wait" and wait.counter == "memory"  # 4 - 2 = 2.0 < 2.4
    never = ledger.admit(req(ram_gb=14.0), T0, spend(), dry=True)
    assert never.kind == "refuse" and never.counter == "memory_never"  # it cannot fit on this host at all
    # a run that has just started is still growing into its memory: it counts on top of what the host reports
    host.avail = 5.5
    ledger.admit(req(ram_gb=2.0), T0, spend())
    assert ledger.admit(req(ram_gb=1.5), T0, spend(), dry=True).counter == "memory"  # 5.5 - 2 - 1.5 < 2.4
    assert ledger.admit(req(ram_gb=1.5), T0 + timedelta(minutes=5), spend(), dry=True).kind == "admit"  # grown by now
    # a system that cannot report memory admits, and the reservation says so
    blind = make(tmp_path / "b", memory=lambda: None)
    d = blind.admit(req(ram_gb=4.0), T0, spend())
    assert d.kind == "admit" and any("memory not reported" in n for n in d.reservation.notes)
    assert "memory" not in d.reservation.counters


def close(ledger, run_id, now):
    """Settle a run by the claim the store holds for it (a test stands in for the run that owns it)."""
    claim = ledger.conn().execute("SELECT claim_id FROM receipts WHERE run_id = ?", (run_id,)).fetchone()[0]
    res = queue.Reservation(run_id, claim, 0, 0, 0, 0, "ui", False, None, "2026-11")
    assert ledger.settle(res, status="ok", usd=0, tokens=None, stop_reason="completed", now=now)


def ui(who, ram, tag):
    return req(job=f"ui-{tag}", lane="ui", who=who, ram_gb=ram, usd=0.0, turns=0, seconds=0.0, cap_usd=None)


def test_join_ui_run_goes_first_and_writer_waits_never_refused(tmp_path):
    clock = Clock()
    total, used = 16.0, {}
    reserve = total * 0.15

    def memory():  # the host's free memory falls as admitted runs take theirs
        return (total - sum(used.values()), total)

    ledger = make(tmp_path, clock=clock, memory=memory, limits=queue.Limits(busy_s=0.2, poll_s=5))
    kinds = []

    def ask(r):
        d = ledger.admit(r, clock(), spend())
        kinds.append(d.kind)
        if d.kind == "admit":
            used[r.run_id] = r.ram_gb
            assert total - sum(used.values()) >= reserve - 1e-9  # free memory never fell under the reserve
        return d

    w1, w2, j = ui("writer", 3, "w1"), ui("writer", 3, "w2"), ui("join", 3, "j")
    assert ask(w1).kind == "admit"
    d = ask(w2)  # the lane holds one, so the writer waits
    assert (d.kind, d.counter) == ("wait", "lane:ui")
    d = ask(j)  # a join arrives after the writer began waiting
    assert (d.kind, d.counter) == ("wait", "lane:ui")
    close(ledger, w1.run_id, clock())
    del used[w1.run_id]
    clock.advance(seconds=5)
    d = ask(w2)  # the writer has waited longer and asks first, but the join goes before it
    assert (d.kind, d.counter) == ("wait", "queue_ahead")
    assert ask(j).kind == "admit"
    close(ledger, j.run_id, clock())
    del used[j.run_id]
    assert ask(w2).kind == "admit"  # and now the writer goes
    close(ledger, w2.run_id, clock())
    del used[w2.run_id]
    # a writer's memory must fit beside the headroom kept for a join: 8 GB free, a writer's 5 GB leaves 3, which is over
    # the 2.4 GB reserve but under the reserve plus the 2 GB headroom. The same run is fine for a join.
    used["something else"] = 8.0
    big_writer, big_join = ui("writer", 5, "bw"), ui("join", 5, "bj")
    d = ask(big_writer)
    assert (d.kind, d.counter) == ("wait", "memory")
    assert ledger.admit(big_join, clock(), spend(), dry=True).kind == "admit"
    assert "refuse" not in kinds  # no writer or join was ever refused for a full lane or memory


def test_a_waiter_that_goes_away_leaves_the_line(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5))
    held = req(job="holder", lane="ui", who="writer", usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    assert ledger.admit(held, clock(), spend()).kind == "admit"
    gone = req(job="gone", lane="ui", who="join", usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    assert ledger.admit(gone, clock(), spend()).kind == "wait"  # it is in line now
    later = req(job="later", lane="ui", who="writer", usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    assert ledger.admit(later, clock(), spend(), dry=True).counter == "lane:ui"
    clock.advance(minutes=5)  # no look from the join for a long while: its place in line has lapsed
    assert ledger.reap(clock()) == 0
    assert (
        ledger.receipt(gone.run_id)["status"] == "skipped"
        and ledger.receipt(gone.run_id)["stop_reason"] == "waiter_gone"
    )


def test_wait_for_slot_waits_and_never_refuses_a_full_lane(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock, limits=queue.Limits(ui_slots=1, poll_s=5))
    held = ledger.admit(
        req(job="holder", lane="ui", who="writer", usd=0.0, turns=0, seconds=0.0, cap_usd=None), clock(), spend()
    ).reservation
    r = req(job="waiter", lane="ui", who="writer", usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    told, slept = [], []

    def sleep(s):
        slept.append(s)
        clock.advance(seconds=s)
        if len(slept) == 3:
            ledger.settle(held, status="ok", usd=0, tokens=None, stop_reason="completed", now=clock())

    got = queue.wait_for_slot(ledger, r, sleep=sleep, notify=told.append)
    assert got.run_id == r.run_id and slept == [5, 5, 5] and [d.counter for d in told] == ["lane:ui"]
    # a timeout is its own error, and the waiter leaves the line
    other = req(job="timeout", lane="ui", who="writer", usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    with pytest.raises(queue.SlotTimeout) as e:
        queue.wait_for_slot(ledger, other, timeout_s=8, sleep=lambda s: clock.advance(seconds=s))
    assert e.value.exit_code == 1 and e.value.details["counter"] == "lane:ui"
    assert ledger.receipt(other.run_id)["stop_reason"] == "gave_up"
    never = req(job="never", lane="ui", who="writer", ram_gb=99.0, usd=0.0, turns=0, seconds=0.0, cap_usd=None)
    with pytest.raises(queue.SlotRefused) as e:  # only a request that can never fit is refused
        queue.wait_for_slot(ledger, never, sleep=lambda s: None)
    assert e.value.exit_code == 3


def test_subscription_limits_are_read_through_admission(tmp_path):
    full = usage.RateLimits(T0, usage.Window(100.0, T0 + timedelta(hours=2)), None)
    ledger = make(tmp_path, rate_limits=lambda now: full)
    d = ledger.admit(req(credential="subscription"), T0, spend())
    assert d.kind == "refuse" and d.counter == "subscription_five_hour"
    assert ledger.admit(req(credential="api_key"), T0, spend()).kind == "admit"  # a key has no subscription window
    unknown = make(tmp_path / "u", rate_limits=lambda now: None)
    d = unknown.admit(req(credential="subscription"), T0, spend())
    assert d.kind == "admit" and any("unknown" in n for n in d.reservation.notes)  # unknown is a note, never room


def test_rate_limit_stale_reads_as_unknown(tmp_path):
    state = tmp_path / "state"
    payload = {
        "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": (T0 + timedelta(hours=3)).timestamp()}}
    }
    assert usage.capture_statusline(json.dumps(payload), state, T0) == "stashed"
    assert stat.S_IMODE(usage.stash_path(state).stat().st_mode) == 0o600
    fresh = usage.read_rate_limits(state, T0 + timedelta(seconds=599))
    assert fresh.five_hour.used_pct == 100 and fresh.seven_day is None and fresh.exhausted() == ["five_hour"]
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=601)) is None  # older than the TTL: unknown
    assert usage.read_rate_limits(state, T0 - timedelta(seconds=5)) is None  # stamped in the future
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=599), ttl_s=60) is None
    assert usage.RATE_TTL_S == 600
    # the ledger's own default reads through the same function and the same TTL
    ledger = queue.Ledger(state, limits=queue.Limits(busy_s=0.2), memory=lambda: None)
    assert (
        ledger.admit(req(credential="subscription"), T0 + timedelta(seconds=599), spend()).counter
        == "subscription_five_hour"
    )
    d = ledger.admit(req(credential="subscription"), T0 + timedelta(seconds=601), spend())
    assert d.kind == "admit" and any("unknown" in n for n in d.reservation.notes)
    # a payload that carries no `rate_limits` at all leaves a good capture alone: it is still read, until the TTL ends it
    assert usage.capture_statusline(json.dumps({"model": "x"}), state, T0 + timedelta(seconds=10)) == "unchanged"
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=11)).five_hour.used_pct == 100
    # one that says it has none (`rate_limits` empty, or no usable window) is a tombstone: unknown, even inside the TTL
    assert usage.capture_statusline(json.dumps({"rate_limits": {}}), state, T0 + timedelta(seconds=12)) == "tombstone"
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=13)) is None
    # a window past its reset time, and a figure that is not a percentage, are dropped
    both = {
        "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": T0.timestamp() + 60},
            "seven_day": {"used_percentage": 1_760_000_000, "resets_at": 1},
        }
    }
    usage.capture_statusline(json.dumps(both), state, T0)
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=30)).seven_day is None
    assert usage.read_rate_limits(state, T0 + timedelta(seconds=120)) is None  # the only window left has reset
    assert usage.capture_statusline("not json", state, T0) is None  # nothing is written for a payload that is not JSON


def test_dedupe_by_message_and_request_id():
    def line(mid, request=None, session="s1", ts="2026-11-08T00:00:00Z", out=10, side=False):
        e = {
            "message": {"id": mid, "usage": {"input_tokens": 5, "output_tokens": out}},
            "sessionId": session,
            "timestamp": ts,
            "isSidechain": side,
        }
        if request:
            e["requestId"] = request
        return e

    same = [line("m1", "r1"), line("m1", "r1", ts="2026-11-08T00:00:05Z")]
    assert len(usage.dedupe_usage(same)) == 1  # one reply written twice
    assert (
        len(usage.dedupe_usage([line("m1", "r1"), line("m1", "r2")])) == 2
    )  # the same id on another request is another reply
    # no requestId: scoped to the session and timestamp
    assert len(usage.dedupe_usage([line("m2"), line("m2")])) == 1
    assert len(usage.dedupe_usage([line("m2"), line("m2", ts="2026-11-08T00:00:09Z")])) == 2
    assert len(usage.dedupe_usage([line("m2"), line("m2", session="s2")])) == 2
    # the main thread beats a sidechain copy; then the larger total wins
    kept = usage.dedupe_usage([line("m3", "r", out=99, side=True), line("m3", "r", out=1)])
    assert kept[0]["isSidechain"] is False
    kept = usage.dedupe_usage([line("m4", "r", out=1), line("m4", "r", out=50)])
    assert kept[0]["message"]["usage"]["output_tokens"] == 50
    assert usage.dedupe_usage([{"message": {}}, {"x": 1}, line("m5", "r")]) == [
        line("m5", "r")
    ]  # no id, no usage to count


@pytest.mark.parametrize(
    ("runner", "reports", "otel"),
    [
        ("claude-code", (100, 40, 10), 150),  # Claude Code's input leaves its cache out
        ("opencode", (100, 40, 10), 150),  # so does OpenCode's
        ("codex", (150, 40, 10), 150),  # Codex's input already counts it
    ],
)
def test_unit_converter_per_runner(runner, reports, otel):
    assert usage.otel_input_tokens(runner, *reports) == otel


def test_the_converter_does_not_guess_a_runner():
    with pytest.raises(KeyError):
        usage.otel_input_tokens("some-new-runner", 1, 1, 1)


# ---- schedules ----------------------------------------------------------------------------------------------------


def test_every_nm_renders_on_both_schedulers(tmp_path):
    when = jobs.parse_schedule("every 10m", "t")
    assert when == jobs.When("every", every=10) and str(when) == "every 10m"
    assert systemd.on_calendar(when) == "*-*-* *:00/10:00"
    assert launchd.interval(when) == [{"Minute": m} for m in (0, 10, 20, 30, 40, 50)]
    sched = jobs.Scheduled("drain", when, ("queue", "run"))
    unit = systemd.render(sched, {"HOME": str(tmp_path)}, ["/usr/bin/ws"])
    assert "OnCalendar=*-*-* *:00/10:00" in unit["ws-drain.timer"] and '"queue" "run"' in unit["ws-drain.service"]
    plist = plistlib.loads(launchd.render(sched, {"HOME": str(tmp_path)}, ["/usr/bin/ws"]).encode())
    assert plist["StartCalendarInterval"] == launchd.interval(when) and plist["ProgramArguments"][-2:] == [
        "queue",
        "run",
    ]
    assert jobs.parse_schedule("every 5m", "t").every == 5 and jobs.parse_schedule("EVERY 30m", "t").every == 30
    for bad in ("every 4m", "every 31m", "every 10", "every m", "every 10m 5m", "every 0m", "every -5m"):
        with pytest.raises(jobs.JobError, match="every|schedule"):
            jobs.parse_schedule(bad, "job x: schedule")


def test_a_queue_drain_job_file_gets_a_timer(tmp_path, monkeypatch):
    home = tmp_path / "wshome"
    (home / "jobs").mkdir(parents=True)
    monkeypatch.setenv("WS_HOME", str(home))
    (home / "jobs" / "drain.toml").write_text(
        'command = "queue"\nschedule = "every 15m"\n' + CONTRACT, encoding="utf-8"
    )
    os.chmod(home / "jobs", 0o700)
    (found,) = jobs.scheduled()
    assert found.argv == ("queue", "run") and found.when.every == 15
    assert jobs.problems() == {}


# ---- job keys -----------------------------------------------------------------------------------------------------


def _job(extra: str = "") -> str:
    return 'runner = "claude-code"\nmodel = "m"\nprompt = "p"\n' + extra


@pytest.mark.parametrize(
    ("extra", "key"),
    [
        ('purpose = "no good"\n', "purpose"),
        ("budget_usd = 0\n", "budget_usd"),
        ("grace_s = -1\n", "grace_s"),
        ("preemptible = 1\n", "preemptible"),
        ("strict = 'yes'\n", "strict"),
        ('deadline = "tomorrow"\n', "deadline"),
        ("deadline = 2026-11-10T09:00:00\n", "deadline"),
        ('checkpoint = "/etc/passwd"\n', "checkpoint"),
        ('checkpoint = "../x"\n', "checkpoint"),
        ("resources = 3\n", "resources"),
        ("[resources]\nram_gb = 0\n", "ram_gb"),
        ("[resources]\nslot = 'a b'\n", "slot"),
        ("[resources]\nram = 2\n", "ram"),
    ],
)
def test_each_new_job_key_error_names_the_key(extra, key, tmp_path):
    with pytest.raises(jobs.JobError, match=key):
        jobs.loads("j", _job(extra), tmp_path)


def test_the_new_job_keys_parse(tmp_path):
    text = _job(
        'purpose = "jobs"\nbudget_usd = 2.5\nstrict = true\npreemptible = true\ngrace_s = 7\ncheckpoint = "p.json"\n'
        'deadline = "2026-11-10T09:00:00Z"\n[resources]\nram_gb = 4\nslot = "ui"\n'
    )
    j = jobs.loads("j", text, tmp_path)
    assert (j.purpose, j.budget_usd, j.strict, j.preemptible, j.grace_s, j.checkpoint) == (
        "jobs",
        2.5,
        True,
        True,
        7,
        "p.json",
    )
    assert j.deadline == datetime(2026, 11, 10, 9, tzinfo=UTC) and j.resources == jobs.Resources(4.0, "ui")
    assert j.max_budget_usd is None  # not a file key
    with pytest.raises(jobs.JobError, match="max_budget_usd"):
        jobs.loads("j", _job("max_budget_usd = 3\n"), tmp_path)
    plain = jobs.loads("j", _job(), tmp_path)
    assert plain.resources == jobs.Resources() and plain.deadline is None and not plain.preemptible


def test_queue_settings_are_checked_and_name_the_key():
    assert queue.Limits.from_profile(None) == queue.Limits()
    assert queue.Limits.from_profile({"roles": {"queue": {"max_concurrent": 2, "reserve_pct": 20}}}).reserve_pct == 20.0
    for table, key in (
        ({"max_concurrent": 0}, "max_concurrent"),
        ({"reserve_pct": 100}, "reserve_pct"),
        ({"nope": 1}, "nope"),
        ({"poll_s": True}, "poll_s"),
    ):
        with pytest.raises(queue.LimitsError, match=key) as e:
            queue.Limits.from_profile({"roles": {"queue": table}})
        assert e.value.exit_code == 2


# ---- retries ------------------------------------------------------------------------------------------------------


def test_transient_backoff_schedule():
    assert [queue.backoff_s(n) for n in range(1, 8)] == [30, 60, 300, 900, 3600, 3600, 3600]
    kw = {"preemptible": False, "deadline": None, "now": T0}
    transient = queue.Outcome(1, "timeout", "timeout")
    acts = [queue.classify(transient, tries=t, **kw) for t in range(4)]
    assert [a.kind for a in acts] == ["retry", "retry", "retry", "fail"]  # three tries, then it stops
    assert [a.delay_s for a in acts[:3]] == [30, 60, 300] and all(a.count_try for a in acts[:3])
    for stop in ("broker_unavailable", "interrupted"):
        assert queue.classify(queue.Outcome(1, "error", stop), tries=0, **kw).kind == "retry"
    for stop in ("runner_error", "not_found", "error", None):
        assert queue.classify(queue.Outcome(1, "error", stop), tries=0, **kw).kind == "fail"  # permanent
    # a retry that would start after the deadline is not made
    soon = queue.classify(transient, tries=0, preemptible=False, deadline=T0 + timedelta(seconds=20), now=T0)
    assert soon.kind == "fail" and "deadline" in soon.why


def test_exit_3_is_never_retried(tmp_path):
    kw = {"tries": 0, "preemptible": True, "deadline": None, "now": T0}
    for out in (
        queue.Outcome(3, "refused", "refused:profile_cap"),
        queue.Outcome(3, "expired", "expired"),
        queue.Outcome(2, "error", "bad_job"),
        queue.Outcome(1, "refused", "refused:secret"),
    ):
        assert queue.classify(out, **kw).kind == "fail"
    # through a drain: the message is failed, no new attempt is made, and no try is counted
    ledger = make(tmp_path)
    ledger.enqueue("j", None, T0)
    calls = []
    done = queue.drain(
        ledger, lambda item: calls.append(item.run_id) or queue.Outcome(3, "refused", "refused:profile_cap")
    )
    assert [d["action"] for d in done] == ["fail"] and len(calls) == 1
    (msg,) = ledger.conn().execute("SELECT status, tries FROM work").fetchall()
    assert (msg["status"], msg["tries"]) == (int(sqlqueue.Status.FAILED), 0)
    assert queue.drain(ledger, lambda item: pytest.fail("ran again")) == []
    assert ledger.receipts(active_only=True) == []  # and the attempt that never ran is not left queued
    assert ledger.receipts(last=5)[0]["status"] == "refused"


def test_143_is_preempted_and_settled(tmp_path):
    soon = T0 + timedelta(minutes=10)
    pre = queue.Outcome(1, "preempted", "preempted")
    assert queue.classify(pre, tries=0, preemptible=True, deadline=None, now=T0).kind == "requeue"
    assert queue.classify(pre, tries=0, preemptible=True, deadline=T0 + timedelta(hours=1), now=T0).kind == "requeue"
    assert (
        queue.classify(pre, tries=0, preemptible=True, deadline=T0 + timedelta(seconds=10), now=T0).kind == "fail"
    )  # no time left
    assert (
        queue.classify(pre, tries=0, preemptible=False, deadline=None, now=T0).kind == "fail"
    )  # it never said it could be rerun
    assert not queue.classify(
        pre, tries=2, preemptible=True, deadline=None, now=soon
    ).count_try  # preemption is not a failure
    # a drain requeues it: one new attempt is waiting, and the message is ready again
    ledger = make(tmp_path)
    ledger.enqueue("j", None, T0)
    seen = []
    done = queue.drain(ledger, lambda item: seen.append(item.run_id) or pre, limit=1, preemptible=lambda job: True)
    assert [d["action"] for d in done] == ["requeue"]
    rows = ledger.conn().execute("SELECT run_id, status FROM receipts ORDER BY queued_at, run_id").fetchall()
    assert [r["status"] for r in rows] == ["queued", "queued"]  # the first attempt's row is left for its run to settle


def test_a_waiting_run_stays_queued_and_does_not_use_up_a_try(tmp_path):
    ledger = make(tmp_path)
    run_id, _, _ = ledger.enqueue("j", None, T0)
    done = queue.drain(ledger, lambda item: queue.Outcome(3, None, None, "concurrency"))
    assert [d["action"] for d in done] == ["wait"]
    (msg,) = ledger.conn().execute("SELECT status, tries FROM work").fetchall()
    assert (msg["status"], msg["tries"]) == (int(sqlqueue.Status.READY), 0)
    assert [r["run_id"] for r in ledger.receipts(last=10)] == [run_id]  # no second receipt for the same wait
    assert queue.drain(ledger, lambda item: pytest.fail("not ready yet")) == []  # it is held back 30 s


def test_a_crashed_drain_gives_its_claim_back(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    ledger.enqueue("j", None, T0)
    assert ledger.engine().pop() is not None  # claimed, then the drain died before it ran anything
    assert queue.drain(ledger, lambda item: pytest.fail("too soon: the claim may still be running")) == []
    clock.advance(seconds=queue.ORPHAN_S + 1)
    seen = []
    queue.drain(ledger, lambda item: seen.append(item) or queue.Outcome(0, "ok", "completed"))
    assert len(seen) == 1 and seen[0].message.tries == 1  # it came back, and the crash cost it a try


# ---- the commands ---------------------------------------------------------------------------------------------------


class Leases:
    def request(self, name, purpose, host, socket=None, ttl_s=None):
        return broker.Lease(lease="lease1", base_url=URL, header="x-api-key", expires_at="2099-01-01T00:00:00Z")

    def release(self, lease, socket=None):
        pass


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "wshome"
    (home / "jobs").mkdir(parents=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    exe = bin_ / "claude"
    exe.write_text(FAKE, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("WS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    for name in ("WS_BROKER_SOCKET", "XDG_STATE_HOME", "WS_GATED_SESSION", "WS_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(broker, "request", Leases().request)
    monkeypatch.setattr(broker, "release", Leases().release)
    (home / "profile.toml").write_text(dumps({"roles": {"budget": {"monthly_cap_usd": 20}}}), encoding="utf-8")
    return home


def write_job(home: Path, name="daily", prompt="ok", secrets=True, extra="") -> None:
    text = f'runner = "claude-code"\nmodel = "claude-haiku-4-5"\nprompt = "{prompt}"\ntimeout_s = 30\n{extra}\n'
    if secrets:
        text += '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n'
    (home / "jobs" / f"{name}.toml").write_text(text, encoding="utf-8")


def call(capsys, *argv):
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_add_list_show_cancel_and_the_json_envelope(box, capsys):
    write_job(box)
    code, env = call(capsys, "queue", "add", "daily", "--key", "once")
    assert code == 0 and env["kind"] == "queue.add" and env["schema"] == output.SCHEMA and env["status"] == "ok"
    run_id = env["data"]["run_id"]
    assert env["data"]["replay"] is False
    code, again = call(capsys, "queue", "add", "daily", "--key", "once")
    assert again["data"]["replay"] is True and again["data"]["run_id"] == run_id  # the same key adds nothing
    code, env = call(capsys, "queue", "list")
    assert env["kind"] == "queue.list" and [r["run_id"] for r in env["data"]] == [run_id]
    assert env["data"][0]["status"] == "queued" and "claim_id" not in env["data"][0]  # a claim never leaves the store
    code, env = call(capsys, "queue", "show", run_id)
    assert env["kind"] == "queue.show" and env["data"]["job"] == "daily" and "claim_id" not in env["data"]
    code, env = call(capsys, "queue", "cancel", run_id)
    assert code == 0 and env["kind"] == "queue.cancel" and env["data"]["status"] == "skipped"
    assert call(capsys, "queue", "list")[1]["data"] == []
    code, env = call(capsys, "queue", "list", "--all")
    assert env["data"][0]["status"] == "skipped" and env["data"][0]["stop_reason"] == "cancelled"
    code, env = call(capsys, "queue", "cancel", run_id)
    assert code == 1 and env["error_kind"] == "not_cancellable"
    code, env = call(capsys, "queue", "show", "nothing")
    assert code == 1 and env["error_kind"] == "not_found"
    assert call(capsys, "queue", "add", "nojob")[1]["error_kind"] == "bad_job"


def test_unknown_purpose_is_a_bad_job(box, capsys):
    write_job(box, extra='purpose = "ghost"')
    (box / "policy.toml").write_text("[budget.purposes]\njobs = 5\n", encoding="utf-8")
    code = cli.main(["run", "daily"])
    err = capsys.readouterr().err
    assert code == 2 and "purpose" in err and "ghost" in err and "[budget.purposes]" in err
    (rec,) = runrecord.read(runrecord.runs_path())
    assert rec["status"] == "error" and rec["job"] == "daily"  # a bad job still leaves its record
    code, env = call(capsys, "queue", "add", "daily")
    assert code == 2 and env["error_kind"] == "bad_job" and "purpose" in env["message"]
    assert not (box / "fake-saw.jsonl").exists()  # the runner never started
    # no [budget.purposes] at all is the same: a name that is not there
    (box / "policy.toml").unlink()
    assert cli.main(["run", "daily"]) == 2


def test_subscription_job_is_refused_in_the_queue(box, capsys):
    write_job(box, "login", secrets=False)
    code, env = call(capsys, "queue", "add", "login")
    assert code == 3 and env["error_kind"] == "subscription_login" and "[[secrets]]" in env["hint"]
    assert call(capsys, "queue", "list", "--all")[1]["data"] == []  # nothing was queued
    # a job that talks to a provider on this machine is not on a login
    write_job(
        box,
        "local",
        secrets=False,
        extra='[runner_options]\nprovider = {options = {baseURL = "http://127.0.0.1:8080/v1"}}',
    )
    local = jobs.load("local")
    assert queue.credential_kind(local) == "local"
    assert queue.credential_kind(jobs.load("login")) == "subscription"
    # and a queued job that has since lost its key is refused when the drain reaches it, not run
    write_job(box, "later")
    run_id = call(capsys, "queue", "add", "later")[1]["data"]["run_id"]
    write_job(box, "later", secrets=False)
    code, env = call(capsys, "queue", "run")
    assert code == 1 and env["data"]["attempts"][0]["action"] == "fail"
    assert not (box / "fake-saw.jsonl").exists()
    assert call(capsys, "queue", "show", run_id)[1]["data"]["status"] == "refused"


def test_queue_run_drains_through_the_same_code_as_ws_run(box, capsys):
    write_job(box, "a")
    write_job(box, "b", extra='purpose = "jobs"\nbudget_usd = 2.0')
    (box / "policy.toml").write_text("[budget.purposes]\njobs = 5\n", encoding="utf-8")
    ids = [call(capsys, "queue", "add", j)[1]["data"]["run_id"] for j in ("a", "b")]
    code, env = call(capsys, "queue", "run")
    assert code == 0 and [a["action"] for a in env["data"]["attempts"]] == ["done", "done"]
    recs = {r["job"]: r for r in runrecord.read(runrecord.runs_path())}
    assert set(recs) == {"a", "b"} and recs["b"]["purpose"] == "jobs" and recs["b"]["schema"] == 3
    assert recs["b"]["reservation"]["usd"] == 2.0 and recs["b"]["stop_reason"] == "completed"
    saw = [json.loads(line) for line in (box / "fake-saw.jsonl").read_text().splitlines()]
    assert "--max-budget-usd" in saw[1]["argv"] and "--max-budget-usd" not in saw[0]["argv"]  # b named its budget
    rows = {r["run_id"]: r for r in call(capsys, "queue", "list", "--all")[1]["data"]}
    assert {r["status"] for r in rows.values()} == {"ok"} and rows[ids[1]]["actual_usd"] == pytest.approx(0.00021)
    assert all(r["run_id"] in ids for r in rows.values()), "each queued run kept the receipt it was added under"
    assert call(capsys, "queue", "run")[1]["data"]["attempts"] == []


def test_a_queued_run_that_cannot_start_yet_waits_and_a_refused_one_is_failed(box, capsys, monkeypatch):
    write_job(box, "a")
    (box / "profile.toml").write_text(
        dumps({"roles": {"budget": {"monthly_cap_usd": 20}, "queue": {"max_concurrent": 1}}}), encoding="utf-8"
    )
    run_id = call(capsys, "queue", "add", "a")[1]["data"]["run_id"]
    other = queue.ledger_for_env()
    held = other.admit(req(job="busy", cap_usd=None, usd=0.0, turns=0, seconds=0.0), datetime.now(UTC), spend())
    assert held.kind == "admit"
    _, env = call(capsys, "queue", "run")
    assert [a["action"] for a in env["data"]["attempts"]] == ["wait"] and not (box / "fake-saw.jsonl").exists()
    assert runrecord.read(runrecord.runs_path()) == []  # a wait is not a run, and leaves no record
    assert call(capsys, "queue", "show", run_id)[1]["data"]["status"] == "queued"


def test_mutating_verbs_refuse_in_a_gated_session(box, capsys, monkeypatch):
    write_job(box)
    run_id = call(capsys, "queue", "add", "daily")[1]["data"]["run_id"]
    monkeypatch.setenv("WS_GATED_SESSION", "1")
    for argv in (
        ("queue", "add", "daily"),
        ("queue", "cancel", run_id),
        ("queue", "run"),
        ("node", "join"),
        ("node", "remove"),
    ):
        code, env = call(capsys, *argv)
        assert code == 3 and env["error_kind"] == "refused" and "gated session" in env["message"], argv
    assert call(capsys, "queue", "list")[0] == 0 and call(capsys, "queue", "show", run_id)[0] == 0  # reading is allowed
    assert call(capsys, "queue", "list")[1]["data"][0]["status"] == "queued"  # and nothing was changed
    assert not any(box.rglob("footprint.jsonl"))  # and join wrote nothing


def test_ws_queue_slot_runs_the_command_and_gives_the_slot_back(box, capfd):
    code = cli.main(
        [
            "queue",
            "slot",
            "--ram-gb",
            "0",
            "--lane",
            "ui",
            "--who",
            "join",
            "--",
            sys.executable,
            "-c",
            "print('inside'); raise SystemExit(7)",
        ]
    )
    out = capfd.readouterr()
    assert code == 7 and "inside" in out.out  # the command's own output and exit code, no envelope
    (row,) = queue.ledger_for_env().receipts(last=5)
    assert (
        row["status"] == "error" and row["lane"] == "ui" and row["stop_reason"] == "command_failed" and row["settled"]
    )
    assert cli.main(["queue", "slot", "--", sys.executable, "-c", "pass"]) == 0
    assert cli.main(["queue", "slot", "--", "no-such-command-here"]) == 127
    assert cli.main(["queue", "slot", "--json"]) == 2  # a slot with nothing to run is a usage error


def test_ws_run_and_the_receipt_agree(box, capsys):
    write_job(box)
    assert cli.main(["run", "daily"]) == 0
    capsys.readouterr()
    (rec,) = runrecord.read(runrecord.runs_path())
    row = queue.ledger_for_env().receipt(rec["run_id"])
    assert (
        row["status"] == "ok"
        and row["actual_usd"] == rec["cost_usd"]
        and row["settled"]
        and row["month"] == rec["started_at"][:7]
    )
    assert (
        rec["reservation"]["usd"] == 1.0
        and rec["reservation"]["resized"] is False
        and "profile_cap" in rec["reservation"]["counters"]
    )
    assert paths.state_dir().joinpath("queue", "queue.db").is_file()


def test_the_statusline_command_stashes_what_it_is_given(box, capsys, monkeypatch):
    import io

    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": (datetime.now(UTC) + timedelta(hours=2)).timestamp()}
        }
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["usage", "statusline"]) == 0
    capsys.readouterr()
    assert usage.read_rate_limits(paths.state_dir()).five_hour.used_pct == 12.5
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert cli.main(["usage", "statusline"]) == 0  # never fails the statusline
    assert usage.read_rate_limits(paths.state_dir()) is not None  # and leaves the last good capture alone


def test_the_runner_only_ever_sees_a_resized_budget_through_the_adapter():
    j = jobs.Job(name="j", runner="claude-code", model="m", prompt="p", max_budget_usd=0.0001)
    argv, _ = claude_code.build_command(j, Path("/x"), None)
    assert argv[argv.index("--max-budget-usd") + 1] == "0.0001"
    assert run.TERM_GRACE_S > 0


# ---- a whole run, end to end ---------------------------------------------------------------------------------------


def saw(home: Path) -> list[dict]:
    path = home / "fake-saw.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def old_record(cost: float, purpose: str | None = None) -> dict:
    now = runrecord.iso(datetime.now(UTC))
    return runrecord.new(job="old", started_at=now, ended_at=now, status="ok", cost_usd=cost, purpose=purpose)


def test_a_resized_reservation_reaches_the_runner_as_its_own_limit(box, capsys):
    write_job(box)
    log = runrecord.runs_path()
    runrecord.append(log, old_record(19.7))
    assert cli.main(["run", "daily"]) == 0
    (argv,) = [s["argv"] for s in saw(box)]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.3000"
    rec = runrecord.read(log)[-1]
    assert rec["reservation"]["resized"] is True and rec["reservation"]["usd"] == pytest.approx(0.3)
    capsys.readouterr()
    runrecord.append(log, old_record(0.5))
    assert cli.main(["run", "daily"]) == 3  # the cap is spent: refused, naming the counter
    err = capsys.readouterr().err
    assert "monthly cap" in err and len(saw(box)) == 1
    last = runrecord.read(log)[-1]
    assert last["stop_reason"] == "refused:profile_cap" and last["status"] == "refused"


def test_a_purpose_cap_refuses_a_run_with_its_own_counter(box, capsys):
    write_job(box, extra='purpose = "evals"')
    (box / "policy.toml").write_text("[budget.purposes]\nevals = 1.5\njobs = 10\n", encoding="utf-8")
    log = runrecord.runs_path()
    runrecord.append(log, old_record(1.5, "evals"))
    assert cli.main(["run", "daily"]) == 3
    assert "purpose evals" in capsys.readouterr().err and saw(box) == []
    rec = runrecord.read(log)[-1]
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:purpose:evals" and rec["purpose"] == "evals"
    write_job(box, extra='purpose = "jobs"')  # the same job on another purpose runs
    assert cli.main(["run", "daily"]) == 0
    assert runrecord.read(log)[-1]["purpose"] == "jobs"


def test_expired_deadline_does_not_run(box, capsys):
    write_job(box, extra='deadline = "2020-01-01T00:00:00Z"')
    assert cli.main(["run", "daily"]) == 3
    assert "deadline" in capsys.readouterr().err and saw(box) == []
    (rec,) = runrecord.read(runrecord.runs_path())
    assert rec["status"] == "expired" and rec["stop_reason"] == "expired" and rec["exit_code"] is None
    (row,) = queue.ledger_for_env().receipts(last=5)
    assert row["status"] == "expired" and row["settled"]
    # a queued run whose deadline has gone by when the drain reaches it is failed, and never retried
    write_job(box, "late")
    run_id = call(capsys, "queue", "add", "late", "--deadline", "2020-01-01T00:00:00Z")[1]["data"]["run_id"]
    _, env = call(capsys, "queue", "run")
    assert [a["action"] for a in env["data"]["attempts"]] == ["fail"] and saw(box) == []
    assert call(capsys, "queue", "show", run_id)[1]["data"]["status"] == "expired"
    assert call(capsys, "queue", "run")[1]["data"]["attempts"] == []


def test_a_store_that_is_locked_never_lets_the_run_start(box, capsys):
    write_job(box)
    profile = {"roles": {"budget": {"monthly_cap_usd": 20}, "queue": {"busy_s": 0.1}}}
    (box / "profile.toml").write_text(dumps(profile), encoding="utf-8")
    assert cli.main(["run", "daily"]) == 0
    holder = sqlite3.connect(paths.state_dir() / "queue" / "queue.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert cli.main(["run", "daily"]) == 3
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "locked" in capsys.readouterr().err and len(saw(box)) == 1  # the runner started once, for the first run
    rec = runrecord.read(runrecord.runs_path())[-1]
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:store_locked" and rec["reservation"] is None
    assert cli.main(["run", "daily"]) == 0  # and the next run finds the store free


def test_143_is_preempted_and_settled_at_actuals(box, capsys):
    write_job(box, prompt="term")
    assert cli.main(["run", "daily"]) == 1
    (rec,) = runrecord.read(runrecord.runs_path())
    assert rec["status"] == "preempted" and rec["stop_reason"] == "preempted"
    ledger = queue.ledger_for_env()
    row = ledger.receipt(rec["run_id"])
    assert row["status"] == "preempted" and row["settled"] and row["actual_usd"] == 1.0  # it started, so the fallback
    assert row["actual_usd"] == runrecord.record_cost(rec, None, None)
    assert ledger.conn().execute("SELECT COUNT(*) FROM receipts WHERE status = 'running'").fetchone()[0] == 0


def test_a_run_that_reports_no_cost_settles_at_the_figure_the_cap_uses(box, capsys):
    write_job(box, prompt="nocost")
    profile = {"roles": {"budget": {"monthly_cap_usd": 20, "fallback_usd_per_mtok": 10}}}
    (box / "profile.toml").write_text(dumps(profile), encoding="utf-8")
    assert cli.main(["run", "daily"]) == 0
    (rec,) = runrecord.read(runrecord.runs_path())
    assert rec["cost_usd"] is None
    row = queue.ledger_for_env().receipt(rec["run_id"])
    assert (
        row["actual_usd"] == pytest.approx(30 / 1_000_000 * 10) == pytest.approx(runrecord.record_cost(rec, None, 10.0))
    )
    # the next admission counts it once, from the log, and not again from the closed reservation
    counted = runrecord.month_spend(runrecord.read(runrecord.runs_path()), None, None, 10.0)
    d = queue.ledger_for_env().admit(
        req(cap_usd=0.0004, usd=1.0), datetime.now(UTC), lambda: queue.Spend(counted), dry=True
    )
    assert d.kind == "admit" and d.reservation.usd == pytest.approx(0.0001)


def test_a_crash_between_reserve_and_start_does_not_strand_the_reservation(box, capsys):
    import subprocess

    write_job(box)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ledger = queue.ledger_for_env()
    stranded = ledger.admit(req(job="daily", cap_usd=20.0), datetime.now(UTC), spend())
    assert stranded.kind == "admit"
    ledger.conn().execute("UPDATE receipts SET owner_pid = ?", (dead.pid,))  # its owner has died
    assert cli.main(["run", "daily"]) == 0  # the same job runs: the dead one is reaped first, not waited for
    recs = runrecord.read(runrecord.runs_path())
    crashed = next(r for r in recs if r["stop_reason"] == "owner_gone")
    assert crashed["status"] == "outcome_unknown" and crashed["job"] == "daily"
    assert crashed["run_id"] == stranded.reservation.run_id
    rows = {r["run_id"]: r for r in queue.ledger_for_env().receipts(last=5)}
    assert rows[crashed["run_id"]]["status"] == "interrupted" and rows[crashed["run_id"]]["record_written"]
    assert {r["status"] for r in rows.values()} == {"interrupted", "ok"}


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc")
def test_a_reused_pid_is_told_apart_by_its_start_time():
    exists, start = queue.proc_probe(os.getpid())
    assert exists is True and start
    assert queue.owner_state(os.getpid(), start) == "alive"
    assert queue.owner_state(os.getpid(), "1") == "gone"  # the pid is running, but it is not the process that reserved
    assert queue.owner_state(os.getpid(), None) == "unknown"  # no start time on record: the lease decides
    assert queue.owner_state(2**22 + 12345, "1") == "gone"


# ---- review fixes: slots, recovery, memory, cancel, and the counters that were not reached --------------------------------


@pytest.fixture(autouse=True)
def no_real_meminfo(monkeypatch):
    """No test here reads the machine's own /proc/meminfo: a ledger that was not given a reader gets this one."""
    monkeypatch.setattr(queue, "read_memory", lambda path="/proc/meminfo": (8.0, 16.0))


def test_a_ledger_with_no_memory_reader_looks_up_the_module_one_when_it_needs_it(tmp_path):
    ledger = queue.Ledger(tmp_path / "state", limits=queue.Limits(busy_s=0.2), rate_limits=lambda now: None)
    assert ledger.memory() == (8.0, 16.0)  # the patched reader, not the host's


def test_read_memory_reads_the_file_it_is_given(tmp_path, monkeypatch):
    monkeypatch.undo()  # this test is about the real reader, pointed at a file of its own
    good = tmp_path / "meminfo"
    good.write_text("MemTotal:       16777216 kB\nMemFree:  1 kB\nMemAvailable:    8388608 kB\n")
    assert queue.read_memory(str(good)) == (8.0, 16.0)
    good.write_text("MemTotal: 16777216 kB\n")  # no MemAvailable
    assert queue.read_memory(str(good)) is None
    assert queue.read_memory(str(tmp_path / "missing")) is None


def test_memory_counts_only_for_a_run_that_declared_ram_gb(tmp_path):
    def never() -> tuple[float, float]:
        raise AssertionError("memory was read for a run that declared none")

    ledger = make(tmp_path, memory=never)
    d = ledger.admit(req(), T0, spend())  # no ram_gb: the host's memory is not asked
    assert d.kind == "admit" and "memory" not in d.reservation.counters and not d.reservation.notes
    tight = make(tmp_path / "t", host=Host(avail=0.1, total=16.0))
    assert tight.admit(req(), T0, spend()).kind == "admit"  # a host nearly out of memory does not hold it up
    assert tight.admit(req(ram_gb=2.0), T0, spend(), dry=True).counter == "memory"  # but a run that needs 2 GB waits


def test_a_crashed_slot_writes_no_record_and_charges_nothing(tmp_path):
    host, sink = Host(), []
    ledger = make(tmp_path, host=host, costing=(2.0, None), sink=lambda rec: sink.append(rec) or True)
    slot = req(owner_pid=4200, usd=0.0, turns=0, seconds=0.0, who="join", lane="ui", ram_gb=1.0, credential="local")
    assert ledger.admit(slot, T0, spend()).kind == "admit"
    host.procs[4200] = (False, None)  # the `ws queue slot` that held it is gone
    assert ledger.reap(T0) == 1 and sink == []  # no run record for a command that was never a job
    row = ledger.receipt(slot.run_id)
    assert (row["status"], row["actual_usd"], row["settled"], row["stop_reason"]) == ("interrupted", 0, True, "crashed")
    # and no money is held or counted against the cap for it
    assert ledger.admit(req(cap_usd=1.0, usd=1.0), T0, spend()).kind == "admit"


def enqueue_and_claim(ledger, **kw):
    run_id, message_id, _ = ledger.enqueue("j", None, T0, **kw)
    assert ledger.engine().pop() is not None  # claimed, and then the drain died
    return run_id, message_id


def message_status(ledger, message_id) -> int:
    return int(ledger.engine().get(message_id).status)


def test_recovery_gives_up_after_the_tries_a_job_is_allowed(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    _, mid = enqueue_and_claim(ledger)
    for crash in range(queue.MAX_RETRIES):
        clock.advance(seconds=queue.ORPHAN_S + 1)
        assert ledger.recover(clock()) == 1, crash  # it comes back, and each crash costs a try
        assert ledger.engine().get(mid).tries == crash + 1
        assert ledger.engine().pop() is not None
    clock.advance(seconds=queue.ORPHAN_S + 1)
    assert ledger.recover(clock()) == 0  # out of tries: not queued again
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    assert queue.drain(ledger, lambda item: pytest.fail("it was given up on")) == []


def test_recovery_does_not_bring_back_what_is_past_its_deadline(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    _, mid = enqueue_and_claim(ledger, deadline=T0 + timedelta(hours=1))
    clock.advance(hours=2)
    assert ledger.recover(clock()) == 0
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    (row,) = ledger.receipts(last=5)
    assert row["status"] == "error" and row["settled"]  # the receipt ends, it does not wait for ever


def test_recovery_requeues_a_preempted_run_only_for_a_preemptible_job(tmp_path):
    for preemptible, want in ((True, sqlqueue.Status.READY), (False, sqlqueue.Status.FAILED)):
        clock = Clock()
        ledger = make(tmp_path / str(preemptible), clock=clock)
        run_id, mid = enqueue_and_claim(ledger)
        ledger.conn().execute(
            "UPDATE receipts SET status = 'preempted', finished_at = 1, settled = 1, stop_reason = 'preempted' WHERE run_id = ?",
            (run_id,),
        )
        clock.advance(seconds=queue.ORPHAN_S + 1)
        assert ledger.recover(clock(), lambda job, p=preemptible: p) == (1 if preemptible else 0)
        assert message_status(ledger, mid) == int(want), preemptible
        if preemptible:
            assert ledger.engine().get(mid).tries == 0  # a preemption is not the job's fault: no try used


def test_recovery_does_not_run_again_a_job_that_finished(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    run_id, mid = enqueue_and_claim(ledger)
    ledger.conn().execute(
        "UPDATE receipts SET status = 'ok', finished_at = 1, settled = 1, stop_reason = 'completed' WHERE run_id = ?",
        (run_id,),
    )
    clock.advance(seconds=queue.ORPHAN_S + 1)
    assert ledger.recover(clock()) == 0  # it ran to the end; the drain died before saying so
    assert message_status(ledger, mid) == int(sqlqueue.Status.DONE)


def test_cancel_leaves_a_message_a_drain_has_claimed(tmp_path):
    ledger = make(tmp_path)
    run_id, mid = enqueue_and_claim(ledger)
    with pytest.raises(output.WsError) as e:
        ledger.cancel(run_id, T0)
    assert e.value.kind == "not_cancellable"
    assert ledger.receipt(run_id)["status"] == "queued" and not ledger.receipt(run_id)["settled"]  # not marked skipped
    assert message_status(ledger, mid) == int(sqlqueue.Status.LOCKED)
    other, _, _ = ledger.enqueue("k", None, T0)
    assert ledger.cancel(other, T0) == "k"  # one nobody has claimed still cancels


def test_turns_and_time_wait_when_full_and_refuse_when_they_can_never_fit(tmp_path):
    limits = queue.Limits(busy_s=0.2, max_open_turns=10, max_open_seconds=100.0)
    ledger = make(tmp_path, limits=limits)
    assert ledger.admit(req(turns=6, seconds=60.0), T0, spend()).kind == "admit"
    turns = ledger.admit(req(turns=5, seconds=10.0), T0, spend(), dry=True)
    assert (turns.kind, turns.counter) == ("wait", "turns") and "6 of 10" in turns.reason
    time_ = ledger.admit(req(turns=1, seconds=50.0), T0, spend(), dry=True)
    assert (time_.kind, time_.counter) == ("wait", "time") and "60 of 100" in time_.reason
    assert ledger.admit(req(turns=4, seconds=40.0), T0, spend(), dry=True).kind == "admit"  # exactly the room left
    never_turns = ledger.admit(req(turns=11, seconds=1.0), T0, spend(), dry=True)
    assert (never_turns.kind, never_turns.counter) == ("refuse", "turns") and "alone needs 11" in never_turns.reason
    never_time = ledger.admit(req(turns=1, seconds=101.0), T0, spend(), dry=True)
    assert (never_time.kind, never_time.counter) == ("refuse", "time")


def test_a_transient_stop_is_retried_through_drain_with_a_backoff(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    run_id, mid, _ = ledger.enqueue("j", None, T0)
    seen = []

    def runner(item):
        seen.append(item.run_id)
        return queue.Outcome(1, "error", "timeout") if len(seen) == 1 else queue.Outcome(0, "ok", "completed")

    first = queue.drain(ledger, runner)
    assert [d["action"] for d in first] == ["retry"] and len(seen) == 1
    msg = ledger.engine().get(mid)
    assert (int(msg.status), msg.tries) == (int(sqlqueue.Status.READY), 1)  # back in the queue, one try used
    assert queue.drain(ledger, runner) == []  # held back for the backoff (30 s)
    clock.advance(seconds=queue.backoff_s(1) + 1)
    second = queue.drain(ledger, runner)
    assert [d["action"] for d in second] == ["done"] and len(seen) == 2
    assert seen[0] == run_id
    assert len(ledger.receipts(last=10)) == 2  # the retry has a receipt of its own
    assert message_status(ledger, mid) == int(sqlqueue.Status.DONE)


# ---- planted failure drills (ar-T3, pc-K22): a killed run is never silent, a normal one never alerts ------------------

HOLDER = """
import os, sys, time
from datetime import UTC, datetime
from ws import queue
state, job, run_id = sys.argv[1:4]
ledger = queue.Ledger(state, limits=queue.Limits(max_concurrent=50, busy_s=10.0), memory=lambda: None,
                      rate_limits=lambda now: None)
d = ledger.admit(queue.Request(job=job, run_id=run_id, cap_usd=100.0, usd=1.0, turns=5, seconds=600.0), datetime.now(UTC),
                 lambda: queue.Spend(0.0, {}))
print(d.kind, flush=True)
time.sleep(120)
"""


def planted_kills(tmp_path, n: int) -> tuple[list[str], float]:
    """`n` real processes each hold a reservation and are killed with SIGKILL: no handler runs, no record is written.
    Returns their run ids and the wall-clock time of the last kill."""
    import signal
    import subprocess
    import time

    ids = [f"20261108T030000Z-{0xA00000 + i:06x}" for i in range(n)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(tmp_path / "state"), f"killed{i}", rid],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i, rid in enumerate(ids)
    ]
    try:
        assert [p.stdout.readline().strip() for p in procs] == ["admit"] * n
    finally:
        for p in procs:
            p.send_signal(signal.SIGKILL)
        for p in procs:
            p.wait()
            p.stdout.close()
    assert all(p.returncode == -signal.SIGKILL for p in procs)
    return ids, time.time()


def alerting(tmp_path, **kw):
    alerts: list[tuple[str, str, str, float]] = []
    sink: list[dict] = []
    import time

    ledger = queue.Ledger(
        tmp_path / "state", limits=queue.Limits(max_concurrent=50, busy_s=10.0), memory=lambda: None,
        rate_limits=lambda now: None, costing=(2.0, None), sink=lambda rec: sink.append(rec) or True,
        on_stop=lambda job, run_id, why: alerts.append((job, run_id, why, time.time())), **kw,
    )  # fmt: skip
    return ledger, alerts, sink


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc: the owner check needs a process table")
def test_planted_sigkill_outcome_unknown(tmp_path):
    ids, _ = planted_kills(tmp_path, 1)
    ledger, _, sink = alerting(tmp_path)
    assert ledger.reap(datetime.now(UTC)) == 1
    (rec,) = sink
    assert (rec["run_id"], rec["status"], rec["stop_reason"]) == (ids[0], "outcome_unknown", "owner_gone")
    assert rec["duration_ms"] > 0 and rec["exit_code"] is None  # ended minus started, not 0
    assert runrecord.record_cost(rec, 2.0, None) == 2.0
    row = ledger.receipt(ids[0])
    assert (row["status"], row["stop_reason"], row["actual_usd"], row["settled"]) == (
        "interrupted",
        "owner_gone",
        2.0,
        True,
    )


def test_planted_sigkill_duration_is_capped_at_the_runs_timeout(tmp_path):
    host = Host()
    clock = Clock()
    ledger, _, sink = alerting(tmp_path, probe=host.probe, clock=clock)
    r = req(owner_pid=4300, seconds=60.0)
    assert ledger.admit(r, T0, spend()).kind == "admit"
    host.procs[4300] = (False, None)
    assert ledger.reap(T0 + timedelta(minutes=30)) == 1  # found long after: it cannot have run past its timeout
    assert sink[0]["duration_ms"] == 60_000 and sink[0]["ended_at"] == "2026-11-08T03:01:00.000Z"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc: the owner check needs a process table")
def test_planted_sigkill_one_alert(tmp_path):
    """ar-T3: 10 planted SIGKILL runs give 10 alerts within 5 minutes, one each, however many processes reap."""
    import time

    ids, killed_at = planted_kills(tmp_path, 10)
    ledger, alerts, sink = alerting(tmp_path)
    other, other_alerts, _ = alerting(tmp_path)  # a second process looking at the same store
    assert ledger.reap(datetime.now(UTC)) == 10
    assert other.reap(datetime.now(UTC)) == 0 and ledger.reap(datetime.now(UTC)) == 0
    assert other.admit(req(cap_usd=100.0), datetime.now(UTC), spend()).kind == "admit"  # admission reaps too
    assert sorted(a[1] for a in alerts) == sorted(ids) and other_alerts == []
    assert all(a[2].startswith("outcome_unknown") for a in alerts)
    assert max(a[3] for a in alerts) - killed_at < 300 and time.time() - killed_at < 300
    assert len(sink) == 10 and {r["status"] for r in sink} == {"outcome_unknown"}


def test_planted_normal_runs_no_alert(tmp_path):
    """ar-T3: 0 false alerts over 50 normal runs, settled directly and through a drain, with reaps between them."""
    ledger, alerts, sink = alerting(tmp_path, clock=Clock())
    for i in range(25):
        r = req(job=f"normal{i}", cap_usd=1000.0)
        d = ledger.admit(r, T0, spend())
        assert d.kind == "admit"
        ledger.reap(T0)
        ledger.settle(d.reservation, status="ok", usd=0.01, tokens=None, stop_reason="completed", now=T0)
    for i in range(25):
        ledger.enqueue(f"queued{i}", None, T0)
    out = queue.drain(ledger, lambda item: queue.Outcome(0, "ok", "completed"))
    assert len(out) == 25 and {d["action"] for d in out} == {"done"}
    assert ledger.reap(T0) == 0
    assert alerts == [] and sink == []


def test_planted_new_status_reads_old_reader(tmp_path):
    """A record with the new status and fields reads through the reader today's ws uses, the new fields ignored; and a
    store reaped by today's ws (receipt `crashed`) is still recovered as an interruption."""
    log = tmp_path / "logs" / "runs.jsonl"
    rec = runrecord.unknown_outcome("20261108T030000Z-0000aa", "j", T0, T0 + timedelta(seconds=9), ws_version="0")
    rec.update(approval={"switch": "act-and-tell", "quote": "go"}, run_key="j:m1", chain_depth=0)
    runrecord.append(log, rec)
    old_keys = runrecord.KEYS[: runrecord.KEYS.index("reservation") + 1]
    (line,) = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    old_view = {k: line[k] for k in old_keys}  # what a reader that knows only the earlier keys takes from it
    assert old_view["status"] == "outcome_unknown" and old_view["duration_ms"] == 9000
    assert old_view["run_id"] == rec["run_id"] and old_view["stop_reason"] == "owner_gone"
    assert queue._recovered_outcome({"status": "interrupted", "stop_reason": "crashed"}).stop_reason == "interrupted"
    assert queue._recovered_outcome({"status": "interrupted", "stop_reason": "owner_gone"}).stop_reason == "interrupted"


def test_planted_sigkill_rollback_still_alerts_once(tmp_path):
    """A reaper whose transaction rolls back after it wrote the record: the next reaper owes the notice, sends it once,
    and writes no second record."""
    host = Host()
    log = tmp_path / "logs" / "runs.jsonl"
    alerts: list[str] = []
    ledger = make(
        tmp_path, host=host, sink=lambda rec: queue._write_record(log, rec),
        recorded=lambda rid: next((r for r in runrecord.read(log) if r["run_id"] == rid), None),
        on_stop=lambda job, run_id, why: alerts.append(run_id),
    )  # fmt: skip
    r = req(owner_pid=4600)
    assert ledger.admit(r, T0, spend()).kind == "admit"
    host.procs[4600] = (False, None)
    with pytest.raises(RuntimeError), queue._transaction(ledger.conn()) as c:
        ledger._reap(c, T0)
        raise RuntimeError("the store went away before the commit")
    assert ledger.receipt(r.run_id)["status"] == "running" and len(runrecord.read(log)) == 1 and alerts == []
    assert ledger.reap(T0) == 1 and ledger.reap(T0) == 0
    assert alerts == [r.run_id] and len(runrecord.read(log)) == 1


def test_planted_sigkill_already_alerted_by_the_scheduler_is_not_told_again(tmp_path):
    host = Host()
    alerts: list[str] = []
    told = tmp_path / "state" / "logs" / "alerts.jsonl"
    ledger = make(
        tmp_path, host=host, sink=lambda rec: True, on_stop=lambda job, run_id, why: alerts.append(job),
        already_told=lambda job, since: queue.unit_alerted(job, since, told),
    )  # fmt: skip
    nightly, other = req(job="nightly", owner_pid=4700), req(job="other", owner_pid=4701)
    for x in (nightly, other):
        assert ledger.admit(x, T0, spend()).kind == "admit"
    told.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"schema": 1, "ts": "2026-11-07T00:00:00.000Z", "event": "unit-failed", "job": "other"},  # an older failure
        {"schema": 1, "ts": "2026-11-08T03:00:05.000Z", "event": "unit-failed", "job": "nightly"},  # this kill
    ]
    told.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n", encoding="utf-8")
    host.procs[4700] = host.procs[4701] = (False, None)
    assert ledger.reap(T0 + timedelta(minutes=1)) == 2
    assert alerts == ["other"]  # nightly's OnFailure= already said it; other's alert was for an earlier run
    assert queue.unit_alerted("x", T0, tmp_path / "missing.jsonl") is False


def test_planted_sigkill_after_an_unrelated_drain_failure_is_still_told(tmp_path):
    """A drain unit fails whenever a queued job exits non-zero. Such a failure, under the drain job's name, says nothing
    about a queued run that is killed later: that run's notice still goes, naming it."""
    host = Host()
    alerts: list[str] = []
    told = tmp_path / "state" / "logs" / "alerts.jsonl"
    ledger = make(
        tmp_path, host=host, sink=lambda rec: True, on_stop=lambda job, run_id, why: alerts.append(job),
        already_told=lambda job, since: queue.unit_alerted(job, since, told),
    )  # fmt: skip
    queued_id, _, _ = ledger.enqueue("nightly", None, T0)
    assert ledger.admit(req(job="nightly", run_id=queued_id, owner_pid=4800), T0, spend()).kind == "admit"
    told.parent.mkdir(parents=True, exist_ok=True)
    line = {"schema": 1, "ts": "2026-11-08T03:00:05.000Z", "event": "unit-failed", "job": "drain"}  # another job failed
    told.write_text(json.dumps(line) + "\n", encoding="utf-8")
    host.procs[4800] = (False, None)
    assert ledger.reap(T0 + timedelta(minutes=1)) == 1
    assert alerts == ["nightly"]


# ---- one net effect (rr-L3, h-L1-17/18, h-L4-5) ----------------------------------------------------------------------


def test_planted_run_key_one_effect(tmp_path):
    """The same (job, trigger) queued again, while it waits, while it runs and after it has finished, runs once."""
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    first = ledger.enqueue("watch", None, T0, trigger="msg-41")
    assert first[2] is False and ledger.enqueue("watch", None, T0, trigger="msg-41") == (first[0], first[1], True)
    assert ledger.enqueue("other", None, T0, trigger="msg-41")[2] is False  # a key is per job
    ran: list[str] = []

    def runner(item):
        ran.append(item.run_key)
        assert ledger.enqueue("watch", None, T0, trigger="msg-41")[2] is True  # again while it runs
        return queue.Outcome(0, "ok", "completed")

    queue.drain(ledger, runner)
    clock.advance(days=40)
    assert ledger.enqueue("watch", None, clock.now, trigger="msg-41")[2] is True  # and long after it finished
    assert queue.drain(ledger, runner) == []
    assert sorted(ran) == ["other:msg-41", "watch:msg-41"]
    rows = [r for r in ledger.receipts(last=10) if r["job"] == "watch"]
    assert len(rows) == 1 and rows[0]["run_key"] == "watch:msg-41"
    with pytest.raises(output.WsError):
        ledger.enqueue("watch", None, T0, key="k", trigger="t")
    with pytest.raises(output.WsError):
        queue.run_key("watch", "two words")


def test_a_reaped_run_carries_its_run_key(tmp_path):
    host = Host()
    sink: list[dict] = []
    ledger = make(tmp_path, host=host, sink=lambda rec: sink.append(rec) or True)
    run_id, _, _ = ledger.enqueue("watch", None, T0, trigger="evt-9")
    ledger.conn().execute(
        "UPDATE receipts SET status = 'running', started_at = ?, owner_pid = 4400, owner_start_time = 'X', "
        "lease_expires_at = ? WHERE run_id = ?",
        (queue.to_ns(T0), queue.to_ns(T0 + timedelta(hours=1)), run_id),
    )
    host.procs[4400] = (False, None)
    assert ledger.reap(T0 + timedelta(seconds=5)) == 1
    assert sink[0]["run_key"] == "watch:evt-9" and sink[0]["duration_ms"] == 5000


def test_planted_stale_holder_no_overwrite(tmp_path):
    """A drain that stalls past its lease comes back after the message was taken from it (reaped as outcome_unknown and
    failed by recovery): it changes neither the receipts nor the message. Its late record is its run's true outcome,
    counted once, with the reaper's line kept as the alert trail."""
    host = Host()
    log = tmp_path / "logs" / "runs.jsonl"
    ledger = make(tmp_path, host=host, sink=lambda rec: queue._write_record(log, rec))
    run_id, mid, _ = ledger.enqueue("j", None, T0, trigger="evt-1")
    stalled = ledger.engine().pop()
    ledger.conn().execute(
        "UPDATE receipts SET status = 'running', started_at = ?, owner_pid = 4500, owner_start_time = 'S', "
        "lease_expires_at = ? WHERE run_id = ?",
        (queue.to_ns(T0), queue.to_ns(T0), run_id),
    )
    host.procs[4500] = (None, None)  # this host cannot tell; the lease has run out, so the run is reaped
    later = T0 + timedelta(hours=25)
    ledger.clock = lambda: later
    assert queue.drain(ledger, lambda item: queue.Outcome(0, "ok", "completed")) == []  # not run again
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    # the stalled holder wakes and finishes the way a drain would
    late = runrecord.new(
        run_id=run_id, job="j", started_at=runrecord.iso(T0), ended_at=runrecord.iso(later), status="ok",
        stop_reason="completed", cost_usd=0.25, ws_version="0",
    )  # fmt: skip
    runrecord.append(log, late)
    before = ledger.receipts(last=10)
    item = queue.Item(stalled, "j", None, None, run_id, ledger, stalled.idempotency_key)
    act = queue.classify(queue.Outcome(0, "ok", "completed"), tries=0, preemptible=False, deadline=None, now=later)
    with pytest.raises(sqlqueue.StaleClaim):
        queue._finish(ledger, ledger.engine(), stalled, item, run_id, queue.Outcome(0, "ok"), act, later, None)
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    assert ledger.receipts(last=10) == before  # no receipt changed
    assert {r["run_id"]: r["status"] for r in before}[run_id] == "interrupted"
    mine = [r for r in runrecord.read(log) if r["run_id"] == run_id]
    assert [r["status"] for r in mine] == ["ok"]  # the run's own late record is its true outcome, counted once
    assert [r["status"] for r in jsonl_lines(log)] == ["outcome_unknown", "ok"]  # the alert trail stays in the file


def jsonl_lines(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_a_drain_whose_claim_was_taken_over_says_superseded_and_goes_on(tmp_path):
    ledger = make(tmp_path)
    ledger.enqueue("a", None, T0)
    ledger.enqueue("b", None, T0)

    def runner(item):
        if item.job == "a":  # while it runs, its claim is taken over (its lease ran out elsewhere)
            ledger.engine().retry(item.message.message_id, item.message.claim_id)
            ledger.engine().pop()
        return queue.Outcome(0, "ok", "completed")

    out = queue.drain(ledger, runner, limit=2)
    assert [(d["job"], d["action"]) for d in out] == [("a", "superseded"), ("b", "done")]


def test_planted_bad_item_to_failed(tmp_path):
    """One unreadable item and one whose run raises are failed with a reason; the good ones either side still run."""
    told: list[tuple[str, str, str]] = []
    ledger = make(tmp_path, on_stop=lambda job, run_id, why: told.append((job, run_id, why)))
    ledger.enqueue("good1", None, T0)
    bad = ledger.engine().put("not json at all")
    ledger.enqueue("raises", None, T0)
    ledger.enqueue("good2", None, T0)
    ran: list[str] = []

    def runner(item):
        if item.job == "raises":
            raise RuntimeError("boom")
        ran.append(item.job)
        return queue.Outcome(0, "ok", "completed")

    out = queue.drain(ledger, runner)
    assert ran == ["good1", "good2"]
    assert [(d["job"], d["action"]) for d in out] == [
        ("good1", "done"), ("unreadable", "fail"), ("raises", "fail"), ("good2", "done"),
    ]  # fmt: skip
    assert message_status(ledger, bad.message_id) == int(sqlqueue.Status.FAILED)
    rows = {r["job"]: r for r in ledger.receipts(last=10)}
    assert (rows["unreadable"]["status"], rows["unreadable"]["stop_reason"]) == ("error", "bad_item")
    assert rows["unreadable"]["note"] == "the queued data is not a job"
    assert rows["raises"]["note"] == "the run raised RuntimeError" and rows["raises"]["settled"]
    assert [t[0] for t in told] == ["unreadable", "raises"] and all(t[2].startswith("bad_item") for t in told)
    assert queue.drain(ledger, runner) == []  # nothing comes back


def test_a_schema_2_store_gains_the_run_key_column(tmp_path):
    ledger = make(tmp_path)
    ledger.enqueue("j", None, T0)
    c = ledger.conn()
    c.execute("ALTER TABLE receipts DROP COLUMN run_key")
    c.execute("PRAGMA user_version = 2")
    ledger.close()
    again = make(tmp_path)
    assert again.enqueue("j", None, T0, trigger="t1")[2] is False
    assert {r["run_key"] for r in again.receipts(last=5)} == {None, "j:t1"}


def test_a_schema_4_store_gains_the_told_at_column(tmp_path):
    """A queue written before told_at: it opens, keeps its rows, gains the column, and a reaped run's notice is
    recorded in it."""
    host = Host()
    ledger = make(tmp_path, host=host, sink=lambda rec: True, on_stop=lambda job, run_id, why: True)
    old_id = ledger.enqueue("j", None, T0)[0]
    c = ledger.conn()
    c.execute("ALTER TABLE receipts DROP COLUMN told_at")
    c.execute("PRAGMA user_version = 4")
    ledger.close()
    again = make(tmp_path, host=host, sink=lambda rec: True, on_stop=lambda job, run_id, why: True)
    cols = {r[1] for r in again.conn().execute("PRAGMA table_info(receipts)")}
    assert "told_at" in cols and again.conn().execute("PRAGMA user_version").fetchone()[0] == queue.SCHEMA_VERSION == 6
    assert again.receipt(old_id) is not None
    r = req(owner_pid=4990)
    assert again.admit(r, T0, spend()).kind == "admit"
    host.procs[4990] = (False, None)
    assert again.reap(T0) == 1
    told = again.conn().execute("SELECT told_at FROM receipts WHERE run_id = ?", (r.run_id,)).fetchone()[0]
    assert told is not None and again.reaped_of(4990, T0 - timedelta(hours=1)) == []  # a direct run, not a queued one


@pytest.mark.parametrize("trigger", [None, "evt-7"])
def test_a_killed_run_is_never_run_again_by_recovery(tmp_path, trigger):
    """It may have acted, and a rerun cannot yet skip what it did, so it is failed with or without a run key."""
    host, clock = Host(), Clock()
    ledger = make(tmp_path, host=host, clock=clock, sink=lambda rec: True)
    run_id, mid, _ = ledger.enqueue("j", None, T0, trigger=trigger)
    ledger.engine().pop()
    ledger.conn().execute(
        "UPDATE receipts SET status = 'running', started_at = ?, owner_pid = 4800, owner_start_time = 'K', "
        "lease_expires_at = ? WHERE run_id = ?",
        (queue.to_ns(T0), queue.to_ns(T0 + timedelta(hours=1)), run_id),
    )
    host.procs[4800] = (False, None)
    clock.advance(seconds=queue.ORPHAN_S + 1)
    ledger.recover(clock.now)
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    assert ledger.receipt(run_id)["stop_reason"] == "owner_gone"


def test_a_run_that_raised_after_admission_is_recorded_and_charged(tmp_path):
    log = tmp_path / "logs" / "runs.jsonl"
    told: list[str] = []
    ledger = make(
        tmp_path, costing=(2.0, None), sink=lambda rec: queue._write_record(log, rec),
        recorded=lambda rid: next((r for r in runrecord.read(log) if r["run_id"] == rid), None),
        on_stop=lambda job, run_id, why: told.append(why),
    )  # fmt: skip
    ledger.enqueue("spender", None, T0)

    def runner(item):
        d = ledger.admit(req(job=item.job, run_id=item.run_id, cap_usd=100.0), T0, spend())
        assert d.kind == "admit"
        raise RuntimeError("after it had started")

    (out,) = queue.drain(ledger, runner)
    assert out["action"] == "fail"
    (rec,) = runrecord.read(log)
    assert (rec["status"], rec["error"]) == ("outcome_unknown", "the run raised RuntimeError")
    row = ledger.receipt(out["run_id"])
    assert (row["actual_usd"], row["settled"], row["note"]) == (2.0, True, "the run raised RuntimeError")
    assert told == ["bad_item: the run raised RuntimeError"]  # one notice, not two


def test_a_store_error_in_a_run_stops_the_drain_and_is_not_one_items_failure(tmp_path):
    ledger = make(tmp_path)
    run_id, mid, _ = ledger.enqueue("j", None, T0)

    def runner(item):
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        queue.drain(ledger, runner)
    assert message_status(ledger, mid) == int(sqlqueue.Status.LOCKED)  # left for recovery, not failed
    assert ledger.receipt(run_id)["status"] == "queued"


def test_a_finished_receipt_keeps_its_note(tmp_path):
    ledger = make(tmp_path)
    ledger.enqueue("j", None, T0)
    msg = ledger.engine().pop()
    with queue._transaction(ledger.conn()) as c:
        rid = ledger.queued_row(c, msg.message_id)
        c.execute(
            "UPDATE receipts SET status = 'ok', finished_at = 1, settled = 1, note = 'its own' WHERE run_id = ?", (rid,)
        )
    queue._fail_bad(ledger, msg, "j", "late", T0, rid)
    assert ledger.receipt(rid)["note"] == "its own"


def test_a_run_that_settled_ok_before_something_raised_is_done(tmp_path):
    told: list[str] = []
    ledger = make(tmp_path, on_stop=lambda job, run_id, why: told.append(why))
    run_id, mid, _ = ledger.enqueue("j", None, T0)

    def runner(item):
        d = ledger.admit(req(job=item.job, run_id=item.run_id, cap_usd=100.0), T0, spend())
        ledger.settle(d.reservation, status="ok", usd=0.1, tokens=None, stop_reason="completed", now=T0)
        raise RuntimeError("after the run had settled")

    assert [d["action"] for d in queue.drain(ledger, runner)] == ["done"]
    assert message_status(ledger, mid) == int(sqlqueue.Status.DONE) and told == []
    assert ledger.receipt(run_id)["status"] == "ok"


def test_a_store_error_before_admission_comes_back_and_runs(tmp_path):
    clock = Clock()
    ledger = make(tmp_path, clock=clock)
    run_id, _, _ = ledger.enqueue("j", None, T0)
    with pytest.raises(sqlite3.OperationalError):
        queue.drain(ledger, lambda item: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    clock.advance(seconds=queue.ORPHAN_S + 1)
    assert ledger.recover(clock.now) == 1
    out = queue.drain(ledger, lambda item: queue.Outcome(0, "ok", "completed"))
    assert [d["action"] for d in out] == ["done"] and out[0]["run_id"] == run_id


def test_a_disk_error_after_admission_is_failed_as_killed_once(tmp_path):
    """The drain exits on the error; recovery reaps the run: one fallback charge, one outcome_unknown, one notice."""
    host, clock = Host(), Clock()
    log = tmp_path / "logs" / "runs.jsonl"
    told: list[str] = []
    ledger = make(
        tmp_path, host=host, clock=clock, costing=(2.0, None), sink=lambda rec: queue._write_record(log, rec),
        recorded=lambda rid: next((r for r in runrecord.read(log) if r["run_id"] == rid), None),
        on_stop=lambda job, rid, why: told.append(why),
    )  # fmt: skip
    run_id, mid, _ = ledger.enqueue("j", None, T0)

    def runner(item):
        assert ledger.admit(req(job=item.job, run_id=item.run_id), T0, spend()).kind == "admit"
        raise OSError("disk full")

    with pytest.raises(OSError):
        queue.drain(ledger, runner)
    row = ledger.receipt(run_id)
    assert row["status"] == "running"
    host.procs[row["owner_pid"]] = (False, None)  # the drain process has exited
    clock.advance(seconds=queue.ORPHAN_S + 1)
    assert ledger.recover(clock.now) == 0
    assert message_status(ledger, mid) == int(sqlqueue.Status.FAILED)
    row = ledger.receipt(run_id)
    assert row["settled"] and row["actual_usd"] == 2.0 and row["stop_reason"] == "owner_gone"
    assert [r["status"] for r in runrecord.read(log)] == ["outcome_unknown"]
    assert len(told) == 1
    assert queue.drain(ledger, lambda item: queue.Outcome(0, "ok", "completed")) == []


# ---- chains and ticks (h-L4-19/20, ls-4) -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["chain:go", "chain:reset"])
def test_planted_chain_cap(box, capsys, mode):
    """Through the queue's drain and a real runner process whose child queues the next link through the ledger: each
    link is one deeper, the one past the cap is refused (exit 3), and a runner that sets its own depth back to 0 does
    not reset the chain, because the ledger takes the depth from the parent's receipt."""
    write_job(box, name="chainer", prompt=mode)
    (box / "profile.toml").write_text(
        dumps({"roles": {"budget": {"monthly_cap_usd": 20}, "queue": {"chain_cap": 2}}}), encoding="utf-8"
    )
    code, _ = call(capsys, "queue", "add", "chainer")
    assert code == 0
    for _ in range(5):
        cli.main(["queue", "run", "--json"])
        capsys.readouterr()
    saw = [json.loads(x) for x in (box / "chain-saw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [s["depth"] for s in saw] == ["0", "1", "2"]
    assert [s["rc"] for s in saw] == [0, 0, queue.EXIT_REFUSED]
    ledger = queue.ledger_for_env()
    rows = sorted(ledger.receipts(last=10), key=lambda r: r["queued_at"])
    assert [r["chain_depth"] for r in rows] == [0, 1, 2]
    assert all(s["parent"] == r["run_id"] for s, r in zip(saw, rows, strict=True))
    recs = [r for r in runrecord.read(runrecord.runs_path()) if r["job"] == "chainer"]
    assert sorted(r["chain_depth"] for r in recs) == [0, 1, 2]  # the normal run record carries it too


def test_a_chain_takes_its_depth_from_the_parent_not_the_caller(tmp_path):
    told: list[str] = []
    ledger = make(tmp_path, limits=queue.Limits(busy_s=0.2, chain_cap=2), on_stop=lambda j, r, why: told.append(why))
    parent, _, _ = ledger.enqueue("loop", None, T0, trigger="a", chain_depth=2)
    with pytest.raises(output.WsError) as e:
        ledger.enqueue("loop", None, T0, trigger="b", chain_depth=0, chain_parent=parent)  # claims 0; it is 3
    assert e.value.kind == "chain_cap" and e.value.exit_code == queue.EXIT_REFUSED
    assert len(told) == 1 and told[0].startswith("chain_cap: 3 jobs deep")
    assert ledger.parent_depth("20261108T030000Z-ffffff") == 0  # a parent the ledger does not know
    assert queue.chain_depth_from_env({}) == 0 and queue.chain_depth_from_env({queue.CHAIN_ENV: "x"}) == 0
    assert queue.chain_parent_from_env({queue.CHAIN_PARENT_ENV: "../x"}) is None
    assert ledger.enqueue("loop", None, T0, trigger="by-hand")[2] is False  # a human action resets the chain


def test_a_watcher_ignores_events_caused_by_ws(tmp_path):
    ledger = make(tmp_path)
    assert ledger.on_event("watch", None, "commit-1", T0, by_ws=True) is None
    first = ledger.on_event("watch", None, "commit-2", T0, by_ws=False)
    assert first is not None and first[2] is False
    assert ledger.on_event("watch", None, "commit-2", T0, by_ws=False)[2] is True  # once per event
    assert [r["run_key"] for r in ledger.receipts(last=5)] == ["watch:commit-2"]


def test_planted_double_tick_runs_once(tmp_path):
    """Two ticks for the same slot, from two processes at the same moment, queue one run; it runs once."""
    slot = T0
    results: list[tuple[str, str | None]] = []
    barrier = threading.Barrier(2)

    def tick():
        ledger = make(tmp_path, limits=queue.Limits(busy_s=5.0))
        barrier.wait()
        results.append(ledger.tick("nightly", None, slot, T0))
        ledger.close()

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] for r in results) == ["already", "queued"]
    ledger = make(tmp_path)
    ran: list[str] = []
    queue.drain(ledger, lambda item: ran.append(item.run_key) or queue.Outcome(0, "ok", "completed"))
    assert ran == ["nightly:tick-20261108T030000Z"]
    assert ledger.tick("nightly", None, slot - timedelta(days=1), T0) == ("already", None)  # an older slot too


def test_a_tick_coalesces_while_a_run_is_active_and_skips_a_missed_slot(tmp_path):
    ledger = make(tmp_path)
    assert ledger.tick("j", None, T0, T0)[0] == "queued"
    assert ledger.tick("j", None, T0 + timedelta(hours=1), T0 + timedelta(hours=1)) == ("coalesced", None)
    assert (
        ledger.tick("j", None, T0 + timedelta(hours=2), T0 + timedelta(hours=2), coalesce_if_active=False)[0]
        == "queued"
    )
    late = T0 + timedelta(hours=3)
    assert ledger.tick("k", None, late, late + timedelta(minutes=30), skip_missed_s=600) == ("missed", None)
    assert ledger.tick("k", None, late, late) == ("already", None)  # the missed slot was still advanced past
    assert (
        ledger.tick("k", None, late + timedelta(hours=1), late + timedelta(hours=1), skip_missed_s=600)[0] == "queued"
    )


def test_ws_queue_tick_command(box, capsys):
    write_job(box)
    code, out = call(capsys, "queue", "tick", "daily", "--slot", "2026-11-10T03:00:00Z")
    assert code == 0 and out["data"]["result"] == "queued"
    code, out = call(capsys, "queue", "tick", "daily", "--slot", "2026-11-10T03:00:00Z")
    assert code == 0 and out["data"]["result"] == "already"


def test_planted_tick_after_a_kill_runs_the_slot(tmp_path):
    """A run of the job killed while it ran leaves a stale `running` receipt; the next tick reaps it first, so its
    slot is queued, not coalesced away, and the kill is told once."""
    host = Host()
    told: list[str] = []
    ledger = make(tmp_path, host=host, sink=lambda rec: True, on_stop=lambda job, run_id, why: told.append(why))
    what, run_id = ledger.tick("nightly", None, T0, T0)
    assert what == "queued"
    ledger.conn().execute(
        "UPDATE receipts SET status = 'running', started_at = ?, owner_pid = 4900, owner_start_time = 'N', "
        "lease_expires_at = ? WHERE run_id = ?",
        (queue.to_ns(T0), queue.to_ns(T0 + timedelta(hours=1)), run_id),
    )
    host.procs[4900] = (False, None)  # SIGKILL: the process is gone
    nxt = T0 + timedelta(days=1)
    assert ledger.tick("nightly", None, nxt, nxt)[0] == "queued"
    assert ledger.receipt(run_id)["stop_reason"] == "owner_gone"
    assert len(told) == 1 and told[0].startswith("outcome_unknown")


# ---- the reaper timer and the timer's tick (C499 items 8 and 10) -----------------------------------------------------


def test_planted_sigkill_with_no_further_admit_is_told_by_one_reaper_tick(box, capsys, monkeypatch):
    """A run SIGKILLed with nothing else starting after it: the reaper timer's `ws queue reap` (every 2 minutes) records
    it and sends its one notice at its next tick, against the real process table; the tick after says nothing."""
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(queue, "notify_stop", lambda job, run_id, reason, **kw: told.append((job, run_id)) or True)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ledger = queue.ledger_for_env()
        r = req(job="nightly", owner_pid=victim.pid)
        assert ledger.admit(r, datetime.now(UTC), spend()).kind == "admit"
        ledger.close()
    finally:
        victim.kill()
        victim.wait()
    code, out = call(capsys, "queue", "reap")
    assert code == 0 and out["data"]["reaped"] == 1 and told == [("nightly", r.run_id)]
    assert out["data"]["told"] == [{"job": "nightly", "run_id": r.run_id}]
    (rec,) = runrecord.read(runrecord.runs_path(dict(os.environ)))
    assert (rec["run_id"], rec["status"]) == (r.run_id, "outcome_unknown")
    code, out = call(capsys, "queue", "reap")
    assert code == 0 and out["data"]["reaped"] == 0 and len(told) == 1


def test_queue_reap_is_refused_in_a_gated_session(box, capsys, monkeypatch):
    monkeypatch.setenv("WS_GATED_SESSION", "1")
    code, out = call(capsys, "queue", "reap")
    assert code == 3 and out["error_kind"] == "refused"


def test_the_timers_slot_is_the_latest_time_of_the_schedule_even_when_it_fires_late(box, capsys, monkeypatch):
    """systemd and launchd do not say which time they fired for, so `--slot schedule` works it out from the job's own
    schedule: a catch-up fire hours late (Persistent=true after the machine was off) still ticks the slot it missed,
    and a second fire for the same slot queues nothing."""
    write_job(box, extra='schedule = "daily 03:00"')
    fired = datetime.now().astimezone().replace(hour=9, minute=41, second=7, microsecond=0)  # six hours late
    monkeypatch.setattr(queue, "datetime", _Frozen(fired.astimezone(UTC)))
    want = runrecord.iso(fired.replace(hour=3, minute=0, second=0).astimezone(UTC))
    code, out = call(capsys, "queue", "tick", "daily", "--slot", "schedule")
    assert code == 0 and out["data"]["slot"] == want and out["data"]["result"] == "queued"
    code, out = call(capsys, "queue", "tick", "daily", "--slot", "schedule", "--no-coalesce")
    assert code == 0 and out["data"]["result"] == "already"  # the same slot: never the minute it fired in


def test_slot_schedule_needs_a_schedule(box, capsys):
    write_job(box)
    code, out = call(capsys, "queue", "tick", "daily", "--slot", "schedule")
    assert code == 2 and "needs a schedule" in out["message"]


def test_the_jobs_own_keys_set_coalescing_and_skipping_and_the_flags_override_them(box, capsys, monkeypatch):
    seen: list[dict] = []
    real = queue.Ledger.tick

    def spy(self, job, purpose, slot, now, **kw):
        seen.append(kw)
        return real(self, job, purpose, slot, now, **kw)

    monkeypatch.setattr(queue.Ledger, "tick", spy)
    write_job(box, extra="coalesce_if_active = false\nskip_missed = 600")
    assert call(capsys, "queue", "tick", "daily", "--slot", "2026-11-10T03:00:00Z")[0] == 0
    assert (seen[-1]["coalesce_if_active"], seen[-1]["skip_missed_s"]) == (False, 600.0)
    write_job(box)  # neither key: today's defaults
    assert call(capsys, "queue", "tick", "daily", "--slot", "2026-11-10T04:00:00Z")[0] == 0
    assert (seen[-1]["coalesce_if_active"], seen[-1]["skip_missed_s"]) == (True, None)
    args = ("--slot", "2026-11-10T05:00:00Z", "--no-coalesce", "--skip-missed", "30")
    assert call(capsys, "queue", "tick", "daily", *args)[0] == 0
    assert (seen[-1]["coalesce_if_active"], seen[-1]["skip_missed_s"]) == (False, 30.0)


def test_tick_run_drains_after_queueing(box, capsys, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(queue, "run_item", lambda item: ran.append(item.job) or queue.Outcome(0, "ok", "completed"))
    write_job(box)
    code = cli.main(["queue", "tick", "daily", "--slot", "2026-11-10T03:00:00Z", "--run"])
    out = capsys.readouterr().out
    assert code == 0 and ran == ["daily"] and "daily 2026-11-10T03:00:00.000Z: queued" in out and "1 attempt" in out


class _Frozen:
    """`queue.datetime` with a fixed now (everything else passes through to the real class)."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now.astimezone(tz) if tz else self._now

    def __getattr__(self, name):
        return getattr(datetime, name)


def test_opening_a_second_ledger_never_closes_a_descriptor_of_the_open_database(tmp_path, monkeypatch):
    """Closing any descriptor of a file drops every POSIX lock the process holds on it, a live connection's too
    (C507: a child that wrote the queue then took itself for the last connection and removed the WAL index)."""
    first = make(tmp_path)
    first.enqueue("j", None, T0)  # the database exists and this process holds a connection to it
    db = os.path.realpath(first.path)
    opened: dict[int, str] = {}
    closed_db: list[int] = []
    real_open, real_close = os.open, os.close

    def spy_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        opened[fd] = os.path.realpath(path)
        return fd

    def spy_close(fd):
        if opened.pop(fd, None) == db:
            closed_db.append(fd)
        return real_close(fd)

    monkeypatch.setattr(queue.os, "open", spy_open)
    monkeypatch.setattr(queue.os, "close", spy_close)
    second = make(tmp_path)
    second.conn()
    assert closed_db == []
    monkeypatch.undo()
    assert first.receipts(last=5) and second.receipts(last=5)
