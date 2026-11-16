"""The failure alert, the dead-man check and the drop-ins: stubs, a fake sender, no systemd and no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from ws import jobs, jobwatch, runrecord
from ws.profile import dumps, profile_path
from ws.roles import ntfy

CONTRACT = (
    'timeout_s = 600\non_failure = "alert"\nlog = "file"\nuninstall = "ws schedule remove x"\n'
    'expected_rate = "1h"\ndata_class = "none"\n'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    (home / "jobs").mkdir(parents=True)
    os.chmod(home / "jobs", 0o700)
    e = {"WS_HOME": str(home), "HOME": str(tmp_path)}
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    return e


def notify(env, text="title"):
    p = profile_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        dumps({"roles": {"notify": {"adapter": "ntfy", "url": "https://ntfy.example.org/t", "alert_text": text}}}),
        encoding="utf-8",
    )


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, url, body, headers):
        self.sent.append(body.decode() if isinstance(body, bytes) else body)
        return 200


def job(env, name="stub", rate="1h"):
    """A scheduled job that runs through `ws run`, so its runs leave records under its name."""
    (jobs.jobs_dir(env) / f"{name}.toml").write_text(
        'runner = "claude-code"\nmodel = "m"\nprompt = "p"\nschedule = "hourly"\n' + CONTRACT.replace("1h", rate),
        encoding="utf-8",
    )


def record(env, name, when):
    rec = runrecord.new(
        run_id=runrecord.new_run_id(when), job=name, started_at=runrecord.iso(when), ended_at=runrecord.iso(when),
        status="ok", stop_reason=None, duration_ms=1,
    )  # fmt: skip
    runrecord.append(runrecord.runs_path(env), rec)


def lines(env):
    return [json.loads(x) for x in jobwatch.alerts_path(env).read_text().splitlines()]


def test_a_failed_unit_writes_one_not_automatic_line_and_one_push(env):
    notify(env)
    s = Sender()
    rec = jobwatch.alert_unit("ws-daily.service", env, status=lambda u: ("exit-code", "3"), sender=s)
    assert rec["job"] == "daily" and rec["pushed"]
    (line,) = lines(env)
    assert line["kind"] == "not-automatic" and line["unit"] == "ws-daily.service" and line["exit_status"] == "3"
    assert len(s.sent) == 1 and "daily" in s.sent[0] and "ws-daily.service" in s.sent[0] and "exit 3" in s.sent[0]


def test_the_alert_carries_no_log_text(env):
    notify(env)
    s = Sender()
    jobwatch.alert_unit("ws-daily.service", env, status=lambda u: ("exit-code", "1"), sender=s)
    assert set(lines(env)[0]) == {"schema", "ts", "kind", "event", "job", "unit", "result", "exit_status", "pushed"}


def test_plain_alert_text_names_no_job(env):
    notify(env, "plain")
    s = Sender()
    jobwatch.alert_unit("ws-secretjob.service", env, status=lambda u: ("exit-code", "1"), sender=s)
    assert s.sent == ["a job failed"]


def test_no_notify_role_is_a_line_only_and_never_raises(env):
    rec = jobwatch.alert_unit("ws-daily.service", env, status=lambda u: ("exit-code", "1"))
    assert rec["pushed"] is False and len(lines(env)) == 1  # nothing was pushed: there is no role


def test_a_bad_unit_name_is_refused(env):
    with pytest.raises(ValueError):
        jobwatch.alert_unit("x; rm -rf", env)


def test_a_job_with_no_run_for_its_limit_is_a_miss_with_one_alert(env):
    notify(env)
    job(env)
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    record(env, "stub", now - timedelta(hours=3))
    s = Sender()
    (miss,) = jobwatch.deadman(env, now, s)
    assert miss["job"] == "stub" and miss["hours_since"] == 3.0 and miss["limit_hours"] == 2.0
    assert len(s.sent) == 1 and lines(env)[0]["event"] == "no-run"


def test_a_job_that_stays_red_alerts_once_not_every_check(env):
    notify(env)
    job(env)
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    record(env, "stub", now - timedelta(hours=3))
    s = Sender()
    for h in range(4):
        assert jobwatch.deadman(env, now + timedelta(hours=h), s)
    assert len(s.sent) == 1


def test_a_job_that_runs_again_and_goes_overdue_again_alerts_again(env):
    notify(env)
    job(env)
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    record(env, "stub", now - timedelta(hours=3))
    s = Sender()
    jobwatch.deadman(env, now, s)
    record(env, "stub", now + timedelta(hours=1))
    assert jobwatch.deadman(env, now + timedelta(hours=2), s) == []  # back in time: clears
    assert jobwatch.deadman(env, now + timedelta(hours=5), s)
    assert len(s.sent) == 2


def test_a_job_in_time_is_no_miss(env):
    job(env)
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    record(env, "stub", now - timedelta(minutes=90))
    assert jobwatch.deadman(env, now, Sender()) == []


def test_a_job_that_never_ran_counts_from_its_file(env):
    job(env)
    future = datetime.now(UTC) + timedelta(hours=5)
    (miss,) = jobwatch.deadman(env, future, Sender())
    assert miss["last_run"] is None


def test_a_job_with_no_expected_rate_is_not_watched(env):
    (jobs.jobs_dir(env) / "x.toml").write_text('command = "backup"\nschedule = "hourly"\n', encoding="utf-8")
    assert jobwatch.deadman(env, datetime.now(UTC) + timedelta(days=9), Sender()) == []


def test_the_cli_exits_3_while_a_job_is_overdue(env, capsys):
    from ws import cli

    job(env)
    assert cli.main(["watch", "deadman"]) == 0
    os.utime(jobs.jobs_dir(env) / "stub.toml", (1, 1))  # the file is old, the job never ran
    assert cli.main(["watch", "deadman"]) == 3
    assert "stub has not run" in capsys.readouterr().out


def test_dropins_set_on_failure_and_nothing_else(tmp_path):
    (f,) = jobwatch.write_dropins(["svc"], tmp_path)
    assert f == tmp_path / "svc.service.d" / "10-ws-alert.conf"
    body = [x for x in f.read_text().splitlines() if x and not x.startswith("#")]
    assert body == ["[Unit]", "OnFailure=ws-alert@%n.service"]
    with pytest.raises(ValueError):
        jobwatch.write_dropins(["../x"], tmp_path)
    with pytest.raises(ValueError):
        jobwatch.write_dropins(["ws-alert@"], tmp_path)


def test_the_push_goes_through_the_ntfy_role(env, monkeypatch):
    notify(env)
    got = []
    monkeypatch.setattr(ntfy, "send", lambda cfg, env, text, title, kind, now, sender: got.append((text, kind)) or "ok")
    assert jobwatch.push(env, "t", "p") and got == [("t", "alert")]


def test_a_launchd_run_that_failed_alerts_from_its_own_exit_path(env, monkeypatch):
    from ws import run

    got = []
    monkeypatch.setattr(jobwatch, "alert_unit", lambda unit, env=None, **kw: got.append((unit, kw["job"])) or {})
    monkeypatch.delenv("WS_ALERT_ON_EXIT", raising=False)
    run._alert_on_exit("stub", 1)  # a systemd run: OnFailure= does it, not ws run
    assert got == []
    monkeypatch.setenv("WS_ALERT_ON_EXIT", "1")
    run._alert_on_exit("stub", 0)
    assert got == []
    monkeypatch.setattr(run, "_told", ["other"])
    run._alert_on_exit("stub", 1)
    assert got == [("ws.job.stub", "stub")]
    monkeypatch.setattr(run, "_told", ["stub"])  # already pushed as a stop: not twice
    run._alert_on_exit("stub", 1)
    assert len(got) == 1


def test_watch_rotate_rotates_an_oversize_log_and_leaves_a_small_one(env, capsys):
    from ws import cli, paths

    log = paths.state_dir(os.environ) / "logs" / "stub.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * 10)
    assert cli.main(["watch", "rotate", "stub"]) == 0 and "under the cap" in capsys.readouterr().out
    from ws import joblog

    joblog.rotate(log, max_bytes=5)
    assert (log.parent / "stub.log.1").exists()


def test_a_job_that_runs_a_ws_command_is_not_measured_by_run_records(env):
    """Command jobs leave no run record under their name: they would be overdue forever."""
    (jobs.jobs_dir(env) / "bk.toml").write_text(
        f'command = "backup"\nschedule = "hourly"\n{CONTRACT}', encoding="utf-8"
    )
    os.utime(jobs.jobs_dir(env) / "bk.toml", (1, 1))
    assert jobwatch.overdue(env, datetime.now(UTC)) == []


def test_overdue_has_its_own_exit_code_and_a_crash_does_not_share_it(env, capsys, monkeypatch):
    from ws import cli
    from ws.roles import systemd

    job(env)
    os.utime(jobs.jobs_dir(env) / "stub.toml", (1, 1))
    assert cli.main(["watch", "deadman"]) == jobwatch.EXIT_OVERDUE == 3
    unit = systemd.render_watch(env, ["/opt/ws/bin/ws"])["ws-watch-deadman.service"]
    assert "SuccessExitStatus=3" in unit and "SuccessExitStatus=1" not in unit
    monkeypatch.setattr(jobwatch, "deadman", lambda *a, **k: (_ for _ in ()).throw(jobs.JobError("boom")))
    assert cli.main(["watch", "deadman"]) == 1  # a failure is exit 1: the unit fails and its OnFailure= alerts


def test_a_failed_send_is_tried_again_at_the_next_check_but_no_role_is_not(env):
    job(env)
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    record(env, "stub", now - timedelta(hours=3))
    jobwatch.deadman(env, now, Sender())  # no notify role: not retried
    notify(env)

    class Down(Sender):
        def __call__(self, url, body, headers):
            raise OSError("down")

    s = Sender()
    jobwatch.deadman(env, now + timedelta(hours=1), s)
    assert s.sent == []  # told already (no role at the time): one alert per last run
    (jobs.jobs_dir(env) / "other.toml").write_text(
        'runner = "claude-code"\nmodel = "m"\nprompt = "p"\nschedule = "hourly"\n' + CONTRACT, encoding="utf-8"
    )
    os.utime(jobs.jobs_dir(env) / "other.toml", (1, 1))
    jobwatch.deadman(env, now + timedelta(hours=2), Down())  # the send fails: not marked told
    jobwatch.deadman(env, now + timedelta(hours=3), s)
    assert len(s.sent) == 1 and "other" in s.sent[0]  # tried again, and went


def test_a_job_file_cannot_take_the_name_of_the_dead_man_unit(env):
    (jobs.jobs_dir(env) / "watch-deadman.toml").write_text(
        f'command = "backup"\nschedule = "hourly"\n{CONTRACT}', encoding="utf-8"
    )
    with pytest.raises(jobs.JobError, match="reserved"):
        jobs.scheduled(env)
    assert "reserved" in jobs.problems(env)["watch-deadman"]


def test_a_stop_under_systemd_is_one_push_not_two(env, monkeypatch):
    from ws import queue, run

    sent = []
    monkeypatch.setattr(queue, "notify_stop", lambda *a, **k: sent.append(a) or True)
    token = run._says_stops.set(True)
    try:
        rec = {"job": "stub", "run_id": "r1", "status": "error", "stop_reason": "x"}
        monkeypatch.setenv("INVOCATION_ID", "abc")
        run._announce_stop(rec)
        assert sent == []  # the unit fails and OnFailure= alerts
        monkeypatch.delenv("INVOCATION_ID")
        monkeypatch.setattr(run, "_told", [])
        run._announce_stop(rec)
        assert len(sent) == 1 and run._told == ["stub"]
        monkeypatch.setattr(run, "_told", [])
        monkeypatch.setattr(queue, "notify_stop", lambda *a, **k: False)  # nothing went out: still not "told"
        run._announce_stop(rec)
        assert run._told == []
    finally:
        run._says_stops.reset(token)


# ---- a killed drain: one notice, naming the job it cut off (C499 item 7) --------------------------------------------


def _drain_with_a_killed_run(env, monkeypatch):
    """A drain job, and a queued run of `nightly` whose process was SIGKILLed. Returns (the dead pid, the run id, the
    jobs the queue told)."""
    from ws import queue

    (jobs.jobs_dir(env) / "drain.toml").write_text(
        'command = "queue"\nschedule = "every 10m"\n' + CONTRACT, encoding="utf-8"
    )
    told: list[str] = []
    monkeypatch.setattr(queue, "notify_stop", lambda job, run_id, reason, **kw: told.append(job) or True)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ledger = queue.ledger_for_env(env)
        run_id, _, _ = ledger.enqueue("nightly", None, datetime.now(UTC))
        req = queue.Request(job="nightly", run_id=run_id, cap_usd=100.0, usd=1.0, owner_pid=victim.pid)
        assert ledger.admit(req, datetime.now(UTC), lambda: queue.Spend(0.0, {})).kind == "admit"
        ledger.close()
    finally:
        victim.kill()
        victim.wait()
    return victim.pid, run_id, told


def test_planted_sigkill_of_the_drain_is_one_notice_naming_the_reaped_job(env, monkeypatch):
    notify(env)
    pid, run_id, told = _drain_with_a_killed_run(env, monkeypatch)
    s = Sender()
    rec = jobwatch.alert_unit("ws-drain.service", env, status=lambda u: ("signal", "9"), sender=s, pid_of=lambda u: pid)
    assert told == ["nightly"] and s.sent == []  # the queue's notice names nightly; the drain's own push is not sent
    assert rec["reaped"] == [{"job": "nightly", "run_id": run_id}] and rec["pushed"] is False
    assert lines(env)[-1]["job"] == "drain" and lines(env)[-1]["reaped"] == rec["reaped"]


def test_a_drain_failure_that_cut_nothing_off_is_pushed_as_usual(env, monkeypatch):
    notify(env)
    (jobs.jobs_dir(env) / "drain.toml").write_text(
        'command = "queue"\nschedule = "every 10m"\n' + CONTRACT, encoding="utf-8"
    )
    s = Sender()
    rec = jobwatch.alert_unit(
        "ws-drain.service", env, status=lambda u: ("exit-code", "1"), sender=s, pid_of=lambda u: 1
    )
    assert len(s.sent) == 1 and "drain failed" in s.sent[0] and rec["pushed"] is True and "reaped" not in rec


def test_a_run_another_process_held_does_not_mute_the_drains_push(env, monkeypatch):
    """The reaper tells the run it found (another process held it), and the drain's own failure is still pushed."""
    notify(env)
    _pid, _run_id, told = _drain_with_a_killed_run(env, monkeypatch)
    s = Sender()
    rec = jobwatch.alert_unit(
        "ws-drain.service", env, status=lambda u: ("exit-code", "1"), sender=s, pid_of=lambda u: 1
    )
    assert told == ["nightly"] and len(s.sent) == 1 and rec["pushed"] is True


def test_a_job_that_ticks_the_queue_is_watched_by_the_deadman(env):
    text = (
        'runner = "claude-code"\nmodel = "m"\nprompt = "p"\nschedule = "hourly"\n' + CONTRACT
        + '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n'
    )  # fmt: skip
    (jobs.jobs_dir(env) / "keyed.toml").write_text(text, encoding="utf-8")
    (s,) = jobs.scheduled(env)
    assert s.argv[:2] == ("queue", "tick") and s.records
    record(env, "keyed", datetime.now(UTC) - timedelta(hours=3))
    assert [m["job"] for m in jobwatch.overdue(env, datetime.now(UTC))] == ["keyed"]


def test_a_kill_the_reaper_timer_told_first_is_not_pushed_again_by_the_drains_alert(env, monkeypatch):
    from ws import queue

    notify(env)
    pid, run_id, told = _drain_with_a_killed_run(env, monkeypatch)
    ledger = queue.ledger_for_env(env)
    assert ledger.reap(ledger.clock()) == 1 and told == ["nightly"]  # the 2-minute reaper got there first
    ledger.close()
    s = Sender()
    rec = jobwatch.alert_unit("ws-drain.service", env, status=lambda u: ("signal", "9"), sender=s, pid_of=lambda u: pid)
    assert s.sent == [] and told == ["nightly"] and rec["reaped"] == [{"job": "nightly", "run_id": run_id}]


def test_a_unit_that_is_not_a_drain_never_asks_for_its_pid(env):
    asked: list[str] = []
    jobwatch.alert_unit(
        "ws-daily.service", env, status=lambda u: ("exit-code", "1"), pid_of=lambda u: asked.append(u) or 1
    )
    assert asked == []
