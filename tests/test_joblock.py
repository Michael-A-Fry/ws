"""The job lock (heartbeat and fence, a stale holder taken over once) and the job log cap."""

from __future__ import annotations

import os
import threading

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


def test_fences_rise_and_a_lost_holder_does_not_unlock_the_new_one(env):
    a = joblock.acquire("j", env)
    old = os.stat(a.path).st_mtime - 1000
    os.utime(a.path, (old, old))  # a stopped beating
    died = []
    b = joblock.acquire("j", env, on_died=lambda n, o: died.append((n, o)))
    assert b.fence == a.fence + 1 and len(died) == 1 and died[0][1]["fence"] == a.fence
    assert a.beat() is False and a.lost  # a sees it lost the lock
    a.release()
    assert b.path.exists()  # a's release left b's lock alone
    b.release()
    assert not b.path.exists()


def test_a_killed_holder_is_taken_over_after_the_stale_limit_with_one_died_record(env):
    h = joblock.acquire("j", env)  # never released: the process is "killed"
    clock = [os.stat(h.path).st_mtime]
    died = []
    with pytest.raises(joblock.Held):
        joblock.acquire("j", env, now=lambda: clock[0] + joblock.STALE_S - 1)
    clock[0] += joblock.STALE_S + 1
    new = joblock.acquire("j", env, now=lambda: clock[0], on_died=lambda *a: died.append(a))
    assert len(died) == 1
    new.release()


def test_a_bad_name_is_refused(env):
    with pytest.raises(ValueError):
        joblock.acquire("../x", env)


def test_the_heartbeat_touches_the_lock(env):
    lock = joblock.acquire("j", env)
    old = os.stat(lock.path).st_mtime - 500
    os.utime(lock.path, (old, old))
    assert lock.beat() and os.stat(lock.path).st_mtime > old + 400
    lock.release()


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


def test_ws_run_skips_when_a_live_run_holds_the_job_and_records_a_died_run_for_a_stale_one(tmp_path, monkeypatch, capsys):
    from ws import cli, runrecord

    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("HOME", str(tmp_path))
    held = joblock.acquire("stub", dict(os.environ))
    assert cli.main(["run", "stub"]) == 0 and "already running" in capsys.readouterr().err
    old = os.stat(held.path).st_mtime - 10 * joblock.STALE_S
    os.utime(held.path, (old, old))
    cli.main(["run", "stub"])  # takes over; the job does not exist, so this run itself errors
    recs = runrecord.read(runrecord.runs_path(dict(os.environ)))
    assert [r["stop_reason"] for r in recs if r["stop_reason"] == "died"] == ["died"]
