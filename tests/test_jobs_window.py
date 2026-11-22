"""`every Nm HH:00-HH:00`: a repeat that runs only inside a window of whole hours, both ends included, and the
`command = "triage"` job that the inbox schedule needs (`ws triage run --job NAME`, settings in its [triage] table)."""

from __future__ import annotations

import os
import plistlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from ws import jobs
from ws.roles import launchd, systemd

NZ = ZoneInfo("Pacific/Auckland")
WINDOW = "every 30m 05:00-22:00"


def at(text: str) -> datetime:
    """An instant written as an Auckland wall-clock time."""
    return datetime.fromisoformat(text).replace(tzinfo=NZ).astimezone(UTC)


def test_the_window_parses_and_prints_back():
    when = jobs.parse_schedule(WINDOW, "t")
    assert when == jobs.When("every", every=30, window=(5, 22)) and str(when) == WINDOW
    assert jobs.parse_schedule("EVERY 30m 5:00-22:00", "t") == when
    assert jobs.parse_schedule("every 30m", "t").window is None  # no window is the old schedule, unchanged


@pytest.mark.parametrize(
    "text",
    [
        "every 30m 05:30-22:00",  # a window is whole hours
        "every 30m 05:00-22:30",
        "every 30m 22:00-05:00",  # it does not cross midnight
        "every 30m 05:00-05:00",
        "every 30m 05:00-24:00",
        "every 30m 05:00",
        "every 30m 05:00-22:00 daily",
        "every 4m 05:00-22:00",  # the minutes rule still holds
        "hourly 05:00-22:00",
    ],
)
def test_a_bad_window_names_the_key(text):
    with pytest.raises(jobs.JobError, match=r"t: "):
        jobs.parse_schedule(text, "t")


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        ("2026-11-23T05:00:00", "2026-11-23T05:00:00"),  # the first run of the day
        ("2026-11-23T05:29:59", "2026-11-23T05:00:00"),
        ("2026-11-23T13:41:00", "2026-11-23T13:30:00"),
        ("2026-11-23T21:59:00", "2026-11-23T21:30:00"),
        ("2026-11-23T22:00:00", "2026-11-23T22:00:00"),  # the last run is at the end hour itself
        ("2026-11-23T22:31:00", "2026-11-23T22:00:00"),  # no run at 22:30: the slot is still 22:00
        ("2026-11-23T23:50:00", "2026-11-23T22:00:00"),
        ("2026-11-23T04:59:00", "2026-11-22T22:00:00"),  # before the first run: yesterday's last
        ("2026-11-23T00:00:00", "2026-11-22T22:00:00"),
    ],
)
def test_the_slot_of_a_fire_is_the_latest_run_of_the_window(now, slot):
    assert jobs.latest_slot(jobs.parse_schedule(WINDOW, "t"), at(now), NZ) == at(slot)


def test_a_fire_before_a_short_window_gets_yesterdays_last_run():
    when = jobs.parse_schedule("every 5m 09:00-10:00", "t")
    assert jobs.latest_slot(when, at("2026-11-23T08:00:00"), NZ) == at("2026-11-22T10:00:00")


def test_systemd_gets_two_calendar_lines_for_the_two_ends():
    when = jobs.parse_schedule(WINDOW, "t")
    assert systemd.on_calendars(when) == ["*-*-* 05..21:00/30:00", "*-*-* 22:00:00"]
    sched = jobs.Scheduled("inbox", when, ("triage", "run", "--job", "inbox"))
    timer = systemd.render(sched, {"HOME": "/h"}, ["/usr/bin/ws"])["ws-inbox.timer"]
    assert "OnCalendar=*-*-* 05..21:00/30:00\nOnCalendar=*-*-* 22:00:00\n" in timer
    assert systemd.on_calendar(jobs.parse_schedule("every 10m", "t")) == "*-*-* *:00/10:00"  # the old form is as it was


def test_launchd_gets_a_calendar_entry_for_each_run():
    when = jobs.parse_schedule(WINDOW, "t")
    runs = launchd.interval(when)
    assert len(runs) == 17 * 2 + 1 and runs[0] == {"Hour": 5, "Minute": 0} and runs[-1] == {"Hour": 22, "Minute": 0}
    assert {"Hour": 21, "Minute": 30} in runs and {"Hour": 22, "Minute": 30} not in runs
    sched = jobs.Scheduled("inbox", when, ("triage", "run", "--job", "inbox"))
    assert (
        plistlib.loads(launchd.render(sched, {"HOME": "/h"}, ["/usr/bin/ws"]).encode())["StartCalendarInterval"] == runs
    )


CONTRACT = (
    'timeout_s = 600\non_failure = "alert"\nlog = "file"\nuninstall = "ws schedule remove inbox"\n'
    'expected_rate = "1d"\ndata_class = "none"\n'
)


def put(tmp_path, body):
    home = tmp_path / "wsh"
    (home / "jobs").mkdir(parents=True, exist_ok=True)
    os.chmod(home / "jobs", 0o700)
    (home / "jobs" / "inbox.toml").write_text(body, encoding="utf-8")
    return {"WS_HOME": str(home), "HOME": str(tmp_path)}


TRIAGE_JOB = f'command = "triage"\nschedule = "{WINDOW}"\n{CONTRACT}[triage]\nmailboxes = ["a"]\n'


def test_a_triage_job_is_scheduled_as_ws_triage_run_for_its_own_file(tmp_path):
    env = put(tmp_path, TRIAGE_JOB)
    assert jobs.problems(env, contract=True) == {}
    (s,) = jobs.scheduled(env)
    assert s.argv == ("triage", "run", "--job", "inbox") and s.when.window == (5, 22) and not s.records


def test_a_triage_job_needs_its_table_and_the_table_needs_the_command(tmp_path):
    env = put(tmp_path, TRIAGE_JOB.replace('[triage]\nmailboxes = ["a"]\n', ""))
    with pytest.raises(jobs.JobError, match="triage is required"):
        jobs.scheduled(env)
    env = put(tmp_path, TRIAGE_JOB.replace('command = "triage"', 'command = "backup"'))
    with pytest.raises(jobs.JobError, match="triage goes only in a job with command = 'triage'"):
        jobs.scheduled(env)
    env = put(tmp_path, TRIAGE_JOB)
    with pytest.raises(jobs.JobError, match="runs a ws command"):
        jobs.load("inbox", env)  # `ws run` is for jobs with a runner
