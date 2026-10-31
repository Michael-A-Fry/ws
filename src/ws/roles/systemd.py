"""Scheduler role, `systemd`: one service and one timer per scheduled job, as `systemd --user` units.

    jobs/<name>.toml  with  schedule = "daily 03:30"   ->   ws-<name>.service + ws-<name>.timer

The service runs `ws run <name>` (or the job's `command`, for example `ws backup run`). Every timer gets
`Persistent=true`, so a run missed while the machine was off happens at the next start; on a machine that stays on it
never fires, so the "stays on?" answer is not needed here.

The files live in `$WS_HOME/jobs/units/`. `activate` runs `systemctl --user link` on them, which puts one symlink per
file in `~/.config/systemd/user` (outside $WS_HOME), then enables the timers. `deactivate` (`disable --now`) takes the
timers and those links away again; `ws schedule remove`, `ws schedule apply` for a job whose schedule is gone, and
`ws uninstall` all call it. Both go through an injectable runner, so tests check the argv and never call systemctl.
"""

from __future__ import annotations

import os
from pathlib import Path

from ws import install
from ws.jobs import DAYS, Scheduled, When
from ws.profile import ws_home
from ws.roles import FAIL, OK, WARN, _schedule

ROLE, NAME = "scheduler", "systemd"
PREFIX = "ws-"


def _quote(word: str, expand: bool = False) -> str:
    """One word of an ExecStart= or Environment= value: double-quoted, with systemd's escapes. `%` is a specifier in
    both; `$` is a variable only in ExecStart= (`expand`): Environment= takes it literally."""
    if _schedule.CONTROL.search(word):
        raise ValueError("a control character cannot go in a unit file")
    esc = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if expand:
        esc = esc.replace("$", "$$")
    return f'"{esc}"'


def on_calendar(when: When) -> str:
    if when.kind == "hourly":
        return "*-*-* *:00:00"
    at = f"{when.hour:02d}:{when.minute:02d}:00"
    return f"*-*-* {at}" if when.kind == "daily" else f"{DAYS[when.weekday].capitalize()} *-*-* {at}"


def render(job: Scheduled, env, ws_cmd: list[str]) -> dict[str, str]:
    """{file name: text} for one job."""
    unit = f"{PREFIX}{job.name}"
    home = os.path.abspath(ws_home(env))
    lines = [f"Environment={_quote('WS_HOME=' + home)}"]
    if path := _schedule.path_value(env):
        lines.append(f"Environment={_quote('PATH=' + path)}")
    exec_start = " ".join(_quote(w, expand=True) for w in [*ws_cmd, *job.argv])
    service = (
        "# Written by ws from the job file's schedule; `ws schedule apply` rewrites it.\n"
        f"[Unit]\nDescription=ws job {job.name}\n\n"
        f"[Service]\nType=oneshot\n{chr(10).join(lines)}\nExecStart={exec_start}\n"
    )
    timer = (
        "# Written by ws from the job file's schedule; `ws schedule apply` rewrites it.\n"
        f"[Unit]\nDescription=ws job {job.name} ({job.when})\n\n"
        f"[Timer]\nOnCalendar={on_calendar(job.when)}\nPersistent=true\n"
        f"Unit={unit}.service\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {f"{unit}.service": service, f"{unit}.timer": timer}


def files_for(env, name: str) -> list[Path]:
    return [_schedule.units_dir(env) / f"{PREFIX}{name}{ext}" for ext in (".service", ".timer")]


def _want(env) -> dict[Path, str]:
    cmd, out = _schedule.ws_command(env), {}
    for job in _schedule.scheduled(env):
        for name, text in render(job, env, cmd).items():
            out[_schedule.units_dir(env) / name] = text
    return out


def orphans(env) -> list[str]:
    """Jobs that have unit files here but no longer have a schedule."""
    keep = {p.name for p in _want(env)}
    files = _schedule.orphans(env, PREFIX, (".service", ".timer"), keep)
    return sorted({p.stem.removeprefix(PREFIX) for p in files})


def check(cfg, osi, env, network):
    if osi.scheduler != NAME:
        return FAIL, f"{NAME} selected but this machine runs {osi.scheduler}"
    try:
        want, gone = _want(env), orphans(env)
    except ValueError as e:
        return FAIL, str(e)
    if gone:
        return (
            WARN,
            f"{NAME}: timer files for {', '.join(gone)}, which no longer have a schedule: run `ws schedule apply`",
        )
    if not want:
        return OK, NAME
    n = len(want) // 2
    jobs_ = f"{n} scheduled job{'' if n == 1 else 's'}"
    old = _schedule.stale(want)
    if old:
        return (
            WARN,
            f"{NAME}: {jobs_}; {len(old)} unit file{'' if len(old) == 1 else 's'} missing or out of date: run `ws schedule apply`",
        )
    return OK, f"{NAME}: {jobs_}, unit files up to date"


def steps(cfg, answers, env):
    cmd = _schedule.ws_command(env)
    out = []
    for job in _schedule.scheduled(env):
        for name, text in render(job, env, cmd).items():
            out.append(
                install.Step(
                    _schedule.units_dir(env) / name, "file", text, f"systemd {name.rsplit('.', 1)[1]} for {job.name}"
                )
            )
    return out


def activate(env, runner=None) -> list[str]:
    """Link the unit files of the jobs that still have a schedule into the user manager, reload it and enable the
    timers. Returns what went wrong."""
    runner = runner or _schedule.run_cmd
    keep = _want(env)
    files = sorted(keep)
    timers = [p for p in files if p.suffix == ".timer"]
    if not files:
        return []
    problems = []
    for argv in (
        ["systemctl", "--user", "link", *map(str, files)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", *(p.name for p in timers)],
    ):
        code, out = runner(argv)
        if code:
            problems.append(f"`{' '.join(argv[:4])}` failed: {out[:200] or code}")
            break
    return problems


def deactivate(names: list[str], env, runner=None) -> list[str]:
    """Stop and unlink the timers of these jobs, then reload. Returns what went wrong."""
    if not names:
        return []
    runner = runner or _schedule.run_cmd
    problems = []
    for argv in (
        ["systemctl", "--user", "disable", "--now", *(f"{PREFIX}{n}.timer" for n in names)],
        ["systemctl", "--user", "daemon-reload"],
    ):
        code, out = runner(argv)
        if code:
            problems.append(f"`{' '.join(argv[:4])}` failed: {out[:200] or code}")
    return problems
