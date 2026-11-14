"""Scheduler role adapters (systemd, launchd) and the `schedule` key of a job file. Units are rendered and written
through install; systemctl and launchctl are never called: a fake runner records the argv."""

from __future__ import annotations

import json
import plistlib

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
    "ws-watch-deadman.service", "ws-watch-deadman.timer",
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
    assert len([p for p in removed.removed if p.name.startswith("ws-")]) == 8
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
        ["systemctl", "--user", "enable", "--now", "ws-daily.timer", "ws-watch-deadman.timer"],
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
    assert len(systemd.activate(env, fake)) == 1
    assert not any("ws-daily" in a for c in fake.calls for a in c)  # the clashing job is left alone


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
    assert [s.path.name for s in steps if s.kind == "file"] == ["ws.job.daily.plist"]
    assert install.apply(steps, env).changes >= 1
    assert install.apply(steps, env).changes == 0
    fake, plist = Fake(), box / ".ws" / "jobs" / "units" / "ws.job.daily.plist"
    assert launchd.activate(env, fake, uid=501) == []
    assert fake.calls == [
        ["launchctl", "bootout", "gui/501/ws.job.daily"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
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
    assert launchd.orphans(env) == ["daily"]


def test_launchd_with_no_jobs_writes_nothing(box, env):
    assert launchd.steps({}, install.Answers(), env) == []


def test_launchd_bootstrap_failure_is_reported(box, write_job, env):
    write_job("daily", AI_JOB)
    install.apply(launchd.steps({}, install.Answers(), env), env)

    def runner(argv):
        return (5, "Bootstrap failed") if argv[1] == "bootstrap" else (113, "")

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
    assert code == 0 and "7 unit file(s)" in out and len(fake.calls) == 3
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
    assert 'ExecStopPost="/opt/ws/bin/ws" "watch" "rotate" "daily"' in svc  # command jobs rotate too
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
    assert set(files) == {"ws-alert@.service", "ws-watch-deadman.service", "ws-watch-deadman.timer"}
    alert, dead = files["ws-alert@.service"], files["ws-watch-deadman.service"]
    assert 'ExecStart="/opt/ws/bin/ws" "watch" "alert" "%i"' in alert and "OnFailure" not in alert  # no loop
    assert 'ExecStart="/opt/ws/bin/ws" "watch" "deadman"' in dead and "SuccessExitStatus=1" in dead
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
