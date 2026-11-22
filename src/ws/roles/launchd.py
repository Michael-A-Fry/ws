"""Scheduler role, `launchd`: one LaunchAgent plist per scheduled job.

    jobs/<name>.toml  with  schedule = "daily 03:30"   ->   ws.job.<name>.plist

The plist uses StartCalendarInterval, which launchd runs late if the Mac was asleep at the time (several missed
firings become one). That is the catch-up a laptop needs, so the "stays on?" answer changes nothing here. A Mac that was powered
off at the time does not catch up: launchd has no setting for it, and RunAtLoad would run the job at every login.
The plist carries `ProcessType=Background` (launchd's share for scheduled work). Swap note: to replace launchd, add a scheduler
adapter beside this one.
A job with a runner runs `ws queue tick <name> --slot schedule --run` (queue the slot it fired for, then drain); one
that would use a subscription login keeps `ws run <name>`. `ws.job.queue-reap` runs `ws queue reap` every two minutes, and `ws.job.watch-panes` runs `ws watch panes` every minute.
`activate` bootstraps the agents into the user's GUI domain through an injectable runner; tests check the argv.
Every plist carries `WsWrittenBy: ws`. `activate` and `deactivate` boot out a loaded `ws.job.*` label only when
`launchctl print` shows it was loaded from a plist ws wrote (one under `jobs/units/`, or one carrying that key); a label
someone else loaded is left alone and named in the problems, as the systemd adapter does with a unit it did not write.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from ws import install, paths
from ws.jobs import Scheduled, When
from ws.roles import FAIL, OK, WARN, _schedule

ROLE, NAME = "scheduler", "launchd"
PREFIX = "ws.job."
WRITTEN_BY = "WsWrittenBy"  # the key in every plist ws writes: the proof a loaded agent is its own


def label(name: str) -> str:
    return f"{PREFIX}{name}"


def interval(when: When) -> dict[str, int] | list[dict[str, int]]:
    """StartCalendarInterval for a repeat time (launchd's Weekday: 0 is Sunday, 1 is Monday). `every Nm` is a list of
    minutes, 0, N, 2N and on to the hour: the same times systemd's `*:00/N` gives; with a window, each of them in
    each hour of it, and the last hour's :00."""
    if when.kind == "hourly":
        return {"Minute": 0}
    if when.kind == "every" and when.window:
        first, last = when.window
        runs = [{"Hour": h, "Minute": m} for h in range(first, last) for m in range(0, 60, when.every)]
        return [*runs, {"Hour": last, "Minute": 0}]
    if when.kind == "every":
        return [{"Minute": m} for m in range(0, 60, when.every)]
    d = {"Hour": when.hour, "Minute": when.minute}
    if when.kind == "monthly":
        return {**d, "Day": when.day}
    return d if when.kind == "daily" else {**d, "Weekday": (when.weekday + 1) % 7}


def render(job: Scheduled, env, ws_cmd: list[str]) -> str:
    environment = paths.env_for(env)
    environment["WS_ALERT_ON_EXIT"] = "1"  # launchd has no OnFailure=: `ws run` sends the alert from its own exit path
    if path := _schedule.path_value(env):
        environment["PATH"] = path
    log = str(_schedule.logs_dir(env) / f"{job.name}.log")
    plist = {
        "Label": label(job.name),
        "ProgramArguments": [*ws_cmd, *job.argv],
        "EnvironmentVariables": environment,
        "StartCalendarInterval": interval(job.when),
        "ProcessType": "Background",  # launchd's low-priority class: the scheduled-work share systemd gets from a slice
        "StandardOutPath": log,
        "StandardErrorPath": log,
        WRITTEN_BY: "ws",
    }
    return plistlib.dumps(plist, sort_keys=True).decode("utf-8")


REAPER = "queue-reap"  # ws.job.queue-reap: `ws queue reap` every 2 minutes (a reserved job name, so no file takes it)
REAP_EVERY_S = 120


def render_reaper(env, ws_cmd: list[str]) -> str:
    """The queue reaper's agent: `ws queue reap` every two minutes, so a run killed with no further admission is told
    in minutes. launchd has no OnFailure=; a reaper that fails is found again at its next fire."""
    environment = paths.env_for(env)
    if path := _schedule.path_value(env):
        environment["PATH"] = path
    log = str(_schedule.logs_dir(env) / f"{REAPER}.log")
    plist = {
        "Label": label(REAPER), "ProgramArguments": [*ws_cmd, "queue", "reap"], "EnvironmentVariables": environment,
        "StartInterval": REAP_EVERY_S, "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log,
        WRITTEN_BY: "ws",
    }  # fmt: skip
    return plistlib.dumps(plist, sort_keys=True).decode("utf-8")


PANES = "watch-panes"  # ws.job.watch-panes: `ws watch panes` every minute (a reserved job name)
PANES_EVERY_S = 60


def render_panes(env, ws_cmd: list[str]) -> str:
    """The pane watch's agent: `ws watch panes` every minute, so a session sitting on a dialog is pushed to the person."""
    environment = paths.env_for(env)
    if path := _schedule.path_value(env):
        environment["PATH"] = path
    log = str(_schedule.logs_dir(env) / f"{PANES}.log")
    plist = {
        "Label": label(PANES), "ProgramArguments": [*ws_cmd, "watch", "panes"], "EnvironmentVariables": environment,
        "StartInterval": PANES_EVERY_S, "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log,
        WRITTEN_BY: "ws",
    }  # fmt: skip
    return plistlib.dumps(plist, sort_keys=True).decode("utf-8")


def files_for(env, name: str) -> list[Path]:
    return [_schedule.units_dir(env) / f"{label(name)}.plist"]


def _want(env) -> dict[Path, str]:
    cmd = _schedule.ws_command(env)
    out = {_schedule.units_dir(env) / f"{label(j.name)}.plist": render(j, env, cmd) for j in _schedule.scheduled(env)}
    if out:  # the reaper goes with the jobs: with none scheduled it is an orphan, switched off and removed like one
        out[_schedule.units_dir(env) / f"{label(REAPER)}.plist"] = render_reaper(env, cmd)
        out[_schedule.units_dir(env) / f"{label(PANES)}.plist"] = render_panes(env, cmd)
    return out


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
    n = sum(p.stem not in (label(REAPER), label(PANES)) for p in want)
    jobs_ = f"{n} scheduled job{'' if n == 1 else 's'}"
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


NOT_FOUND = 113  # `launchctl print`'s exit for a label the domain does not hold


class PrintFailed(Exception):
    """`launchctl print` failed for a reason other than "not found", so whether ws loaded the label is unknown."""


def _loaded_from(domain: str, lbl: str, runner) -> tuple[bool, str | None]:
    """(loaded?, the plist path `launchctl print` names for it). Not loaded (exit 113) is (False, None); loaded with no
    path line it can read is (True, None). Any other failure raises PrintFailed."""
    code, out = runner(["launchctl", "print", f"{domain}/{lbl}"])
    if code == NOT_FOUND:
        return False, None
    if code:
        raise PrintFailed(f"`launchctl print` failed for {lbl}: {out[:200] or code}; left alone")
    for line in out.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key == "path" and value.strip():
            return True, value.strip()
    return True, None


def _is_own(env, path: str | None) -> bool:
    """True when this plist is one ws wrote: it sits under this config folder's `jobs/units/` (links resolved), or it
    reads as a plist carrying `WsWrittenBy: ws` (an agent loaded from a home that has since moved)."""
    if not path:
        return False
    p = Path(path)
    for folder in {_schedule.units_dir(env), Path(os.path.realpath(_schedule.units_dir(env)))}:
        if p.is_relative_to(folder) or Path(os.path.realpath(p)).is_relative_to(folder):
            return True
    try:
        data = plistlib.loads(p.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    return isinstance(data, dict) and data.get(WRITTEN_BY) == "ws"


def _foreign(env, domain: str, lbl: str, runner) -> tuple[bool, str]:
    """(loaded?, a problem line when the label is loaded from a plist ws did not write or `launchctl print` failed,
    else "")."""
    try:
        loaded, path = _loaded_from(domain, lbl, runner)
    except PrintFailed as e:
        return True, str(e)
    if not loaded or _is_own(env, path):
        return loaded, ""
    return True, f"{lbl} is loaded from {path or 'a plist launchctl does not name'}, which ws did not write: left alone"


def activate(env, runner=None, uid: int | None = None) -> list[str]:
    """Load every agent ws wrote: bootout first (so a changed plist is picked up), then bootstrap. A label someone else
    has loaded is left alone and named. Returns what went wrong."""
    runner = runner or _schedule.run_cmd
    domain = f"gui/{os.getuid() if uid is None else uid}"
    problems = []
    for plist in sorted(_want(env)):
        loaded, clash = _foreign(env, domain, plist.stem, runner)
        if clash:
            problems.append(clash)
            continue
        if loaded:
            runner(["launchctl", "bootout", f"{domain}/{plist.stem}"])
        code, out = runner(["launchctl", "bootstrap", domain, str(plist)])
        if code:
            problems.append(f"`launchctl bootstrap` failed for {plist.stem}: {out[:200] or code}")
    return problems


def deactivate(names: list[str], env, runner=None, uid: int | None = None) -> list[str]:
    """Boot out these jobs' agents, each only if `launchctl print` shows ws loaded it from a plist of its own. A label
    that is not loaded needs nothing; one someone else loaded is left alone and named. Returns what went wrong."""
    runner = runner or _schedule.run_cmd
    domain = f"gui/{os.getuid() if uid is None else uid}"
    problems = []
    for n in names:
        loaded, clash = _foreign(env, domain, label(n), runner)
        if clash:
            problems.append(clash)
        if clash or not loaded:
            continue
        code, out = runner(["launchctl", "bootout", f"{domain}/{label(n)}"])
        if code:
            problems.append(f"`launchctl bootout` failed for {label(n)}: {out[:200] or code}")
    return problems
