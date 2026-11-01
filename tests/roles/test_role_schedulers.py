"""Scheduler role adapters (systemd, launchd) and the `schedule` key of a job file. Units are rendered and written
through install; systemctl and launchctl are never called: a fake runner records the argv."""

from __future__ import annotations

import json
import plistlib

import pytest

from ws import cli, install, jobs, osinfo, roles
from ws.roles import _schedule, launchd, systemd

WS = ["/opt/ws/bin/ws"]
AI_JOB = 'runner = "claude-code"\nmodel = "m1"\nprompt = "Summarise."\nschedule = "daily 03:30"\n'
BACKUP_JOB = 'command = "backup"\nschedule = "weekly mon 09:05"\n'


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
        "ws-backup.service",
        "ws-backup.timer",
        "ws-daily.service",
        "ws-daily.timer",
    ]
    assert all(s.path.parent == box / ".ws" / "jobs" / "units" for s in steps)
    r = install.apply(steps, env)
    assert len(r.created) >= 4
    assert install.apply(steps, env).changes == 0  # a second run reports 0 changes
    fake = Fake()
    removed = install.uninstall(env, deactivate=lambda paths, e: roles.deactivate_units(paths, e, fake))
    assert not list((box / ".ws" / "jobs" / "units").glob("ws-*"))
    assert len([p for p in removed.removed if p.name.startswith("ws-")]) == 4
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
        ["systemctl", "--user", "link", str(units / "ws-daily.service"), str(units / "ws-daily.timer")],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "ws-daily.timer"],
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
    assert plist["EnvironmentVariables"] == {"WS_HOME": env["WS_HOME"], "PATH": "/usr/bin:/bin"}
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
    assert code == 0 and "2 unit file(s)" in out and len(fake.calls) == 3
    assert fake.calls[0][:3] == ["systemctl", "--user", "link"]
    code, out, _ = run(capsys, "schedule", "apply")
    assert code == 0 and "0 changes" in out

    fake.calls.clear()
    code, out, _ = run(capsys, "schedule", "remove", "daily")
    assert code == 0 and "removed 2 unit file(s)" in out
    assert fake.calls[0] == ["systemctl", "--user", "disable", "--now", "ws-daily.timer", "ws-daily.service"]
    assert not list((box / ".ws" / "jobs" / "units").glob("ws-*"))
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
    assert enable[-1:] == ["ws-other.timer"]  # only the job that still has a schedule is switched on
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
