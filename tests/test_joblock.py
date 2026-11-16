"""The job lock (heartbeat and fence, a stale holder taken over once) and the job log cap."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from ws import joblock, joblog


@pytest.fixture
def env(tmp_path):
    return {"WS_HOME": str(tmp_path / "ws"), "HOME": str(tmp_path)}


def test_a_second_runner_does_not_start_the_job(env):
    first = joblock.acquire("j", env)
    with pytest.raises(joblock.Held):
        joblock.acquire("j", env)
    first.release()
    joblock.acquire("j", env).release()  # free again


def test_a_double_tick_runs_once(env):
    ran = []
    barrier = threading.Barrier(2)

    def tick():
        barrier.wait()
        with joblock.hold("j", env) as lock:
            if lock is not None:
                ran.append(1)
                barrier.reset()
                threading.Event().wait(0.3)  # still running while the other tick arrives

    ts = [threading.Thread(target=tick) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert ran == [1]


def test_fences_rise_and_a_clean_release_leaves_no_died_record(env):
    died = []
    a = joblock.acquire("j", env, on_died=lambda *x: died.append(x))
    a.release()
    b = joblock.acquire("j", env, on_died=lambda *x: died.append(x))
    assert b.fence == a.fence + 1 and died == []
    b.release()


def test_a_killed_holder_is_taken_over_with_one_died_record(env):
    h = joblock.acquire("j", env)  # the lock file says "running"
    os.close(h.fd)  # the process dies: the kernel drops the flock, nothing is written
    died = []
    new = joblock.acquire("j", env, on_died=lambda *a: died.append(a))
    assert len(died) == 1 and died[0][1]["fence"] == h.fence and new.fence == h.fence + 1
    assert new.owns() and not h.owns()
    new.release()
    joblock.acquire("j", env, on_died=lambda *a: died.append(a)).release()
    assert len(died) == 1  # a clean end is not a death


def test_a_bad_name_is_refused(env):
    with pytest.raises(ValueError):
        joblock.acquire("../x", env)


def test_a_log_over_the_cap_rotates_and_keeps_three(tmp_path):
    log = tmp_path / "j.log"
    for i in range(5):
        log.write_bytes(b"x" * 200 + str(i).encode())
        assert joblog.rotate(log, max_bytes=100, keep=3)
    assert log.read_bytes() == b""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["j.log", "j.log.1", "j.log.2", "j.log.3"]
    assert joblog.rotate(log, max_bytes=100) is False  # under the cap: left alone


def test_the_info_floor_drops_debug_and_trace_lines_from_old_logs(tmp_path):
    log = tmp_path / "j.log"
    log.write_bytes(b"INFO start\nDEBUG noisy\n[trace] x\nWARN odd\nplain line\n")
    joblog.rotate(log, max_bytes=1)
    assert (tmp_path / "j.log.1").read_bytes() == b"INFO start\nWARN odd\nplain line\n"


def test_ws_run_skips_when_a_live_run_holds_the_job_and_records_a_died_run_for_a_dead_one(
    tmp_path, monkeypatch, capsys
):
    from ws import cli, runrecord

    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("HOME", str(tmp_path))
    rid = runrecord.new_run_id()
    held = joblock.acquire("stub", dict(os.environ), run_id=rid)
    assert cli.main(["run", "stub"]) == 0 and "already running" in capsys.readouterr().err
    os.close(held.fd)  # the holder dies: the kernel drops the lock
    cli.main(["run", "stub"])  # takes over; the job does not exist, so this run itself errors
    recs = runrecord.read(runrecord.runs_path(dict(os.environ)))
    (dead,) = [r for r in recs if r["status"] == "outcome_unknown"]
    assert dead["run_id"] == rid and dead["stop_reason"] == "owner_gone" and dead["duration_ms"] >= 0


def test_the_lock_file_says_when_and_for_which_run_it_was_taken(env):
    t = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
    lock = joblock.acquire("j", env, run_id="20261117T030000Z-abcdef", now=t)
    on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
    assert on_disk["running"] is True and on_disk["run_id"] == "20261117T030000Z-abcdef"
    assert joblock.started_of(on_disk) == t
    lock.release()
    after = json.loads(lock.path.read_text(encoding="utf-8"))
    assert after["running"] is False and after["run_id"] == "20261117T030000Z-abcdef"  # a clean end keeps them


def test_an_older_lock_file_without_a_start_still_reads(env):
    path = joblock.locks_dir(env) / "j.lock"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fence": 4, "pid": 1, "running": True}), encoding="utf-8")
    died = []
    lock = joblock.acquire("j", env, on_died=lambda *a: died.append(a))
    assert lock.fence == 5 and len(died) == 1 and joblock.started_of(died[0][1]) is None
    lock.release()


def _stub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("HOME", str(tmp_path))
    jobs_dir = tmp_path / "ws" / "jobs"
    jobs_dir.mkdir(parents=True, mode=0o700)
    (jobs_dir / "nightly.toml").write_text(
        'runner = "claude-code"\nmodel = "m"\nprompt = "p"\ntimeout_s = 600\n', encoding="utf-8"
    )
    (jobs_dir / "nightly.toml").chmod(0o600)


def test_a_dead_holder_gets_the_reapers_record_shape_with_its_real_duration(tmp_path, monkeypatch):
    from ws import run, runrecord

    _stub_env(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    rid = runrecord.new_run_id(now)
    run._died(
        "nightly",
        {"fence": 3, "running": True, "run_id": rid, "started_at": runrecord.iso(now - timedelta(seconds=90))},
    )
    (rec,) = runrecord.read(runrecord.runs_path(dict(os.environ)))
    same = runrecord.unknown_outcome(rid, "nightly", now - timedelta(seconds=90), now)
    assert {k: rec[k] for k in ("run_id", "job", "status", "stop_reason")} == {
        k: same[k] for k in ("run_id", "job", "status", "stop_reason")
    }
    assert 89_000 <= rec["duration_ms"] <= 120_000 and "fence 3" in rec["error"]
    # the run started long ago: it could not have been running past its 600 s timeout
    run._died(
        "nightly",
        {
            "fence": 4,
            "running": True,
            "run_id": runrecord.new_run_id(),
            "started_at": runrecord.iso(now - timedelta(hours=5)),
        },
    )
    assert runrecord.read(runrecord.runs_path(dict(os.environ)))[-1]["duration_ms"] == 600_000


def test_a_dead_holder_the_reaper_already_recorded_is_not_recorded_twice(tmp_path, monkeypatch):
    from ws import run, runrecord

    _stub_env(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    rid = runrecord.new_run_id(now)
    log = runrecord.runs_path(dict(os.environ))
    runrecord.append(log, runrecord.unknown_outcome(rid, "nightly", now, now))  # the reaper got there first
    run._died("nightly", {"fence": 1, "running": True, "run_id": rid, "started_at": runrecord.iso(now)})
    assert [r["run_id"] for r in runrecord.read(log)] == [rid]


def test_a_dead_holder_from_an_older_lock_file_has_no_duration(tmp_path, monkeypatch):
    from ws import run, runrecord

    _stub_env(tmp_path, monkeypatch)
    run._died("nightly", {"fence": 2, "pid": 1, "running": True})
    (rec,) = runrecord.read(runrecord.runs_path(dict(os.environ)))
    assert rec["status"] == "outcome_unknown" and rec["duration_ms"] is None
