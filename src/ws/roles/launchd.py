"""Scheduler role, `launchd`: one LaunchAgent plist per scheduled job.

    jobs/<name>.toml  with  schedule = "daily 03:30"   ->   ws.job.<name>.plist

The plist uses StartCalendarInterval, which launchd runs late if the Mac was asleep at the time (several missed
firings become one). That is the catch-up a laptop needs, so the "stays on?" answer changes nothing here. A Mac that was powered
off at the time does not catch up: launchd has no setting for it, and RunAtLoad would run the job at every login.
`activate` bootstraps the agents into the user's GUI domain through an injectable runner; tests check the argv.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from ws import install
from ws.jobs import Scheduled, When
from ws.profile import ws_home
from ws.roles import FAIL, OK, WARN, _schedule

ROLE, NAME = "scheduler", "launchd"
PREFIX = "ws.job."


def label(name: str) -> str:
    return f"{PREFIX}{name}"


def interval(when: When) -> dict[str, int]:
    """StartCalendarInterval for a repeat time (launchd's Weekday: 0 is Sunday, 1 is Monday)."""
    if when.kind == "hourly":
        return {"Minute": 0}
    d = {"Hour": when.hour, "Minute": when.minute}
    return d if when.kind == "daily" else {**d, "Weekday": (when.weekday + 1) % 7}


def render(job: Scheduled, env, ws_cmd: list[str]) -> str:
    home = os.path.abspath(ws_home(env))
    environment = {"WS_HOME": home}
    if path := _schedule.path_value(env):
        environment["PATH"] = path
    log = str(_schedule.logs_dir(env) / f"{job.name}.log")
    plist = {
        "Label": label(job.name),
        "ProgramArguments": [*ws_cmd, *job.argv],
        "EnvironmentVariables": environment,
        "StartCalendarInterval": interval(job.when),
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(plist, sort_keys=True).decode("utf-8")


def files_for(env, name: str) -> list[Path]:
    return [_schedule.units_dir(env) / f"{label(name)}.plist"]


def _want(env) -> dict[Path, str]:
    cmd = _schedule.ws_command(env)
    return {_schedule.units_dir(env) / f"{label(j.name)}.plist": render(j, env, cmd) for j in _schedule.scheduled(env)}


def orphans(env) -> list[str]:
    """Jobs that have a plist here but no longer have a schedule."""
    keep = {p.name for p in _want(env)}
    files = _schedule.orphans(env, PREFIX, (".plist",), keep)
    return sorted(p.stem.removeprefix(PREFIX) for p in files)


def check(cfg, osi, env, network):
    if osi.scheduler != NAME:
        return FAIL, f"{NAME} selected but this machine runs {osi.scheduler}"
    try:
        want, gone = _want(env), orphans(env)
    except ValueError as e:
        return FAIL, str(e)
    if gone:
        return WARN, f"{NAME}: agents for {', '.join(gone)}, which no longer have a schedule: run `ws schedule apply`"
    if not want:
        return OK, NAME
    jobs_ = f"{len(want)} scheduled job{'' if len(want) == 1 else 's'}"
    old = _schedule.stale(want)
    if old:
        return (
            WARN,
            f"{NAME}: {jobs_}; {len(old)} plist{'' if len(old) == 1 else 's'} missing or out of date: run `ws schedule apply`",
        )
    return OK, f"{NAME}: {jobs_}, plists up to date"


def steps(cfg, answers, env):
    out = [install.Step(_schedule.logs_dir(env), "dir", None, "the folder scheduled jobs write their output to")]
    out += [
        install.Step(p, "file", text, f"launchd agent for {p.stem.removeprefix(PREFIX)}")
        for p, text in _want(env).items()
    ]
    return out if len(out) > 1 else []


def activate(env, runner=None, uid: int | None = None) -> list[str]:
    """Load every agent ws wrote: bootout first (so a changed plist is picked up), then bootstrap. Returns what went wrong."""
    runner = runner or _schedule.run_cmd
    domain = f"gui/{os.getuid() if uid is None else uid}"
    problems = []
    for plist in sorted(_want(env)):
        runner(["launchctl", "bootout", f"{domain}/{plist.stem}"])  # not loaded yet is not a problem
        code, out = runner(["launchctl", "bootstrap", domain, str(plist)])
        if code:
            problems.append(f"`launchctl bootstrap` failed for {plist.stem}: {out[:200] or code}")
    return problems


def deactivate(names: list[str], env, runner=None, uid: int | None = None) -> list[str]:
    runner = runner or _schedule.run_cmd
    domain = f"gui/{os.getuid() if uid is None else uid}"
    problems = []
    for n in names:
        code, out = runner(["launchctl", "bootout", f"{domain}/{label(n)}"])
        if code:
            problems.append(f"`launchctl bootout` failed for {label(n)}: {out[:200] or code}")
    return problems
