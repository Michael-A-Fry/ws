"""Scheduler role, `systemd`: one service and one timer per scheduled job, as `systemd --user` units.

    jobs/<name>.toml  with  schedule = "daily 03:30"   ->   ws-<name>.service + ws-<name>.timer

The service runs `ws run <name>` (or the job's `command`, for example `ws backup run`). Every timer gets
`Persistent=true`, so a run missed while the machine was off happens at the next start; on a machine that stays on it
never fires, so the "stays on?" answer is not needed here.

Every service also gets `TimeoutStartSec=` (the job's `timeout_s` and a margin, so a hung run is killed; `infinity`
for a file with no `timeout_s`, which is what a oneshot had before, so an upgrade never kills a running job; the
contract check still flags the file), `Slice=ws-jobs.slice` (a shared slice with a CPU
and memory share, so scheduled work cannot starve the desk) and, for a job with `log = "file"`, `StandardOutput=append:`
a file in the state logs folder (`ws run` rotates it: 20 MB x 3). The first line of each file says it is generated and
how to regenerate it.

Swap note: systemd is adopted as it is (OnFailure=, TimeoutStartSec=, Slice=); to swap the scheduler out, add a
scheduler adapter in this folder (see `launchd.py`) and select it; no list to edit.

The files live in `<config>/jobs/units/`. `activate` runs `systemctl --user link` on them, which puts one symlink per
file in `~/.config/systemd/user` (outside ws's folders), then enables the timers. `deactivate` (`disable --now`, for the
timers and the services) takes those links away again, so none is left dangling; `ws schedule remove`, `ws schedule apply` for a job whose schedule is gone, and
`ws uninstall` all call it. Both go through an injectable runner, so tests check the argv and never call systemctl.
"""

from __future__ import annotations

from pathlib import Path

from ws import install, paths
from ws.jobs import DAYS, Scheduled, When
from ws.roles import FAIL, OK, WARN, _schedule

ROLE, NAME = "scheduler", "systemd"
PREFIX = "ws-"
MARKER = "# Written by ws"  # first line of every unit ws writes: the proof a unit in the user folder is its own
SLICE = "ws-jobs.slice"  # the shared slice every scheduled service runs in
CPU_WEIGHT, MEMORY_HIGH = 50, "75%"  # the share scheduled jobs get: below the default CPUWeight of 100, soft memory cap
TIMEOUT_MARGIN_S = 30  # TimeoutStartSec= is the job's limit plus this, so `ws run` ends its own run first


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
    if when.kind == "every":
        return f"*-*-* *:00/{when.every}:00"
    at = f"{when.hour:02d}:{when.minute:02d}:00"
    return f"*-*-* {at}" if when.kind == "daily" else f"{DAYS[when.weekday].capitalize()} *-*-* {at}"


def _header(source: str) -> str:
    return f"{MARKER}: DO NOT EDIT. Generated from {source}; `ws schedule apply` rewrites it.\n"


def render(job: Scheduled, env, ws_cmd: list[str]) -> dict[str, str]:
    """{file name: text} for one job."""
    unit = f"{PREFIX}{job.name}"
    lines = [f"Environment={_quote(f'{k}={v}')}" for k, v in paths.env_for(env).items()]
    if path := _schedule.path_value(env):
        lines.append(f"Environment={_quote('PATH=' + path)}")
    exec_start = " ".join(_quote(w, expand=True) for w in [*ws_cmd, *job.argv])
    source = f"jobs/{job.name}.toml"
    out = ""
    if job.log == "file":
        out = f"StandardOutput=append:{_schedule.logs_dir(env) / (job.name + '.log')}\nStandardError=inherit\n"
    service = (
        _header(source)
        + f"[Unit]\nDescription=ws job {job.name}\n\n"
        + f"[Service]\nType=oneshot\nSlice={SLICE}\n{chr(10).join(lines)}\nExecStart={exec_start}\n"
        + f"TimeoutStartSec={'infinity' if job.timeout_s is None else int(job.timeout_s) + TIMEOUT_MARGIN_S}\n{out}"
    )
    timer = (
        _header(source)
        + f"[Unit]\nDescription=ws job {job.name} ({job.when})\n\n"
        + f"[Timer]\nOnCalendar={on_calendar(job.when)}\nPersistent=true\n"
        + f"Unit={unit}.service\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {f"{unit}.service": service, f"{unit}.timer": timer}


def render_slice() -> str:
    """The shared slice: a CPU share and a soft memory cap for every scheduled service."""
    return (
        _header("ws's scheduler role")
        + f"[Unit]\nDescription=ws scheduled jobs\n\n[Slice]\nCPUWeight={CPU_WEIGHT}\nMemoryHigh={MEMORY_HIGH}\n"
    )


def files_for(env, name: str) -> list[Path]:
    return [_schedule.units_dir(env) / f"{PREFIX}{name}{ext}" for ext in (".service", ".timer")]


def _want(env) -> dict[Path, str]:
    cmd, out = _schedule.ws_command(env), {}
    for job in _schedule.scheduled(env):
        for name, text in render(job, env, cmd).items():
            out[_schedule.units_dir(env) / name] = text
    if out:
        out[_schedule.units_dir(env) / SLICE] = render_slice()
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
    n = sum(p.suffix == ".timer" for p in want)
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
    if out:
        out.append(
            install.Step(_schedule.units_dir(env) / SLICE, "file", render_slice(), "systemd slice for scheduled jobs")
        )
        out.append(
            install.Step(_schedule.logs_dir(env), "dir", None, "the folder scheduled jobs write their output to")
        )
    return out


def _skip_foreign(env, files: list[Path]) -> tuple[set[str], list[str]]:
    """(the file names to leave out, what to say) for any job whose unit name the user folder already holds for a unit
    ws didn't write. Both files of such a job are left out."""
    clash = sorted({p.stem for p in files if _schedule.is_foreign(env, p.name, MARKER)})
    left_out = {p.name for p in files if p.stem in clash}
    said = [
        f"{stem}.service or {stem}.timer already exists in {_schedule.user_unit_dir(env)} and isn't ws's, "
        f"so ws skipped job {stem.removeprefix(PREFIX)}: rename that unit or the job"
        for stem in clash
    ]
    return left_out, said


def activate(env, runner=None) -> list[str]:
    """Link the unit files of the jobs that still have a schedule into the user manager, reload it and enable the
    timers. A name the user folder already holds for a unit ws didn't write is left alone, its job is skipped and the
    answer says so. Returns what went wrong."""
    runner = runner or _schedule.run_cmd
    keep = _want(env)
    files = sorted(keep)
    left_out, problems = _skip_foreign(env, files)
    files = [p for p in files if p.name not in left_out]
    timers = [p for p in files if p.suffix == ".timer"]
    if not timers:  # every job clashed: the shared slice alone is not worth linking
        return problems
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
    """Stop and unlink the timers of these jobs, then reload. A unit of that name that ws didn't write is left alone and
    named in the answer. Returns what went wrong."""
    if not names:
        return []
    runner = runner or _schedule.run_cmd
    units = [f"{PREFIX}{n}{ext}" for n in names for ext in (".timer", ".service")]
    foreign = [u for u in units if _schedule.is_foreign(env, u, MARKER)]
    problems = [f"{u} in {_schedule.user_unit_dir(env)} isn't ws's, so ws left it alone" for u in foreign]
    units = [u for u in units if u not in foreign]
    if not units:
        return problems
    for argv in (
        ["systemctl", "--user", "disable", "--now", *units],
        ["systemctl", "--user", "daemon-reload"],
    ):
        code, out = runner(argv)
        if code:
            problems.append(f"`{' '.join(argv[:4])}` failed: {out[:200] or code}")
    return problems
