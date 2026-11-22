"""Job files: `jobs/<name>.toml` in ws's config folder, one headless job each, run with `ws run <name>`.

    runner = "claude-code"              adapter name (see `ws gate generate --help`)
    model = "claude-haiku-4-5"          optional: the smallest model that passes. Left out, `jobs.load` takes the
                                        profile's [models].<purpose>, else [models].default (`ws config model`); a run
                                        with none of the three is refused at start, naming the job
    prompt = "..."                      or prompt_file = "daily.md" (relative to the jobs folder)
    cwd = "~/work/thing"                default: an empty folder made for the run
    timeout_s = 900
    max_turns = 12                      optional
    bare = true                         headless default: no hooks, plugins or auto-discovered context
    allow_unpriced = false              run even if the runner cannot price the model (counted at your fallback)
    green = "targeted"                  optional: what the job's verification covers (targeted | package | full |
                                        merge_ready); written to the run record as `green`
    sources = ["notes"]                 policy sources the job reads (the Rule of Two looks at these)
    schedule = "daily 03:30"            optional: hourly | daily HH:MM | weekly DAY HH:MM | monthly D HH:MM (D from 1
                                        to 28) | every Nm (N from 5 to 30), in local time. `ws schedule apply` turns it
                                        into a systemd timer or a launchd agent. The timer queues the slot and drains the
                                        queue (`ws queue tick NAME --slot schedule --run`): the slot is the latest time
                                        of the schedule at or before the fire, so a late catch-up fire gets its own slot.
    coalesce_if_active = true           a tick while a run of the job waits or runs queues nothing (default true;
                                        `ws queue tick --no-coalesce` overrides it for one tick)
    skip_missed = 3600                  optional: a tick more than this many seconds after its slot is skipped, not run
                                        (default: never skipped; `ws queue tick --skip-missed S` overrides it)
    command = "backup"                  instead of runner, model and prompt: a ws command the schedule runs
                                        (`ws backup run`, `ws queue run`, or `script`). Only `ws schedule` runs these;
                                        `ws run` refuses them.
    extra_sources = ["~/.ws"]           with command = "backup": more folders or files the backup takes, beside the
                                        memory, notes and profile (absolute, or under ~)
    script = "~/bin/upkeep.sh"          with command = "script": the reviewed script the timer runs (`ws schedule
    script_sha256 = "<64 hex>"          script NAME`), absolute or under ~, yours and closed to other writers, and its
                                        sha256: a script that no longer hashes to it is refused, so a change needs a
                                        new review (and, for a pinned job, a new `ws job add`)

    purpose = "jobs"                    optional: the [budget.purposes] name this job's spend counts against
    budget_usd = 2.0                    optional: the money reserved for one run (default: the profile's unknown_run_usd)
    strict = false                      true: refuse when the room left is under budget_usd, never run on less
    parent_run = "20261108T030000Z-0a1b2c"
                                        optional: the id of a run in progress whose reservation this run spends from
                                        (`ws run JOB --parent ID` sets it for one run). The run may reserve only what the
                                        parent has left, and must also fit the monthly and purpose caps; a parent that
                                        has finished, has nothing left or is itself a child (one level only) refuses it.
    max_turns, timeout_s                the turns and seconds reserved are these
    deadline = "2026-11-10T09:00:00Z"   optional: a UTC time; a run that would start after it ends as `expired`
    preemptible = false                 true: a SIGTERM (exit 143) is settled and the job requeued, not failed
    grace_s = 10                        optional: seconds between SIGTERM and SIGKILL when the runner is stopped
    checkpoint = "progress.json"        optional: a file, relative to cwd, the job resumes from; the runner gets it as
                                        $WS_CHECKPOINT (use it with a cwd, so the file outlives the run)

    [resources]                         optional: what admission reserves on the host
    ram_gb = 4                          memory the job will use (default 0)
    slot = "run"                        the lane it takes a slot in (default run; ui holds one at a time)

    [[secrets]]                         the provider credential, leased through the broker
    name = "anthropic"
    host = "api.anthropic.com"
    purpose = "run"

    [runner_options]                    adapter-specific (claude-code: allowed_tools, env; opencode: provider, env)

The job files and their folder must belong to you and not be writable by anyone else: a job decides what a runner is
started with, so `ws run` refuses one that someone else could have changed. `prompt_file` must be inside the folder.

A job put in place by `ws job add` (ws.jobcmd) is pinned: `.pins.json` in the jobs folder holds the sha256 of its file
and its prompt_file, and `ws run`, the timers and doctor refuse a pinned job whose files no longer hash to the pin.
A job written by hand and never added has no pin and loads as before. A pins file that cannot be read refuses every job.

    [the run contract]                  a job file with a `schedule` must also carry (`jobs.contract_gaps` finds the gaps, `ws config`
                                        and `ws doctor` warn; `ws run` and the timer still run what loads):
    timeout_s = 900                     1. maximum runtime, written in the file (a unit kills the run at this limit)
    on_failure = "alert"                2. failure hook: "alert" (the unit's OnFailure= runs the alert unit)
    log = "file"                        3. where output goes: "file" (size-capped, in the state logs folder) or "journal"
    uninstall = "ws schedule remove X"  4. the command that takes the job off the machine
    expected_rate = "1d"                5. the gap between runs the job should keep: 30m, 6h, 1d, 7d (the dead-man check
                                        raises an alert when no run record is newer than twice this)
    data_class = "internal"             6. the data class the job works in (a name from the policy's classes, or "none")
    A job with a runner must also set max_turns and budget_usd (the turn limit and the spend cap for one run).

Every error names the key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import adapters, paths, profile
from ws.secrets import HOST_RE, NAME_RE

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED = {"watch-deadman", "queue-reap"}  # the scheduler role's own units: `ws-watch-deadman`, `ws-queue-reap`
DEFAULT_TIMEOUT_S = 900
KEYS = {
    "runner",
    "model",
    "prompt",
    "prompt_file",
    "cwd",
    "timeout_s",
    "max_turns",
    "bare",
    "allow_unpriced",
    "green",
    "sources",
    "secrets",
    "runner_options",
    "schedule",
    "command",
    "resources",
    "preemptible",
    "grace_s",
    "checkpoint",
    "deadline",
    "purpose",
    "budget_usd",
    "strict",
    "parent_run",
    "on_failure",
    "log",
    "uninstall",
    "expected_rate",
    "data_class",
    "coalesce_if_active",
    "skip_missed",
    "extra_sources",
    "script",
    "script_sha256",
}
CONTRACT_KEYS = ("timeout_s", "on_failure", "log", "uninstall", "expected_rate", "data_class")  # the six lines
ON_FAILURE = ("alert",)
LOG_KINDS = ("file", "journal")
_RATE = re.compile(r"^(\d{1,4})([mhd])$")
_RATE_UNITS = {"m": 60, "h": 3600, "d": 86400}
GREEN_LEVELS = ("targeted", "package", "full", "merge_ready")  # as runrecord.GREEN_LEVELS
COMMANDS = {
    "backup": ("backup", "run"),
    "queue": ("queue", "run"),
    "script": ("schedule", "script"),  # the job's name follows: `ws schedule script NAME`
}  # the ws commands a job file may schedule in place of a runner
COMMAND_ONLY = {"extra_sources": "backup", "script": "script", "script_sha256": "script"}  # key -> the command it needs
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_EVERY = re.compile(r"^(\d{1,2})m$")
MONTH_DAY_MAX = 28  # `monthly D`: a day every month has
EVERY_MIN, EVERY_MAX = 5, 30  # `every Nm`: minutes between runs
RESOURCE_KEYS = {"ram_gb", "slot"}


class JobError(Exception):
    kind = "bad_job"


class NoModelError(JobError):
    """A job names no model and the profile's [models] table gives none for it. Loading for a run raises this; the
    scans behind `ws job list`, `ws config` and doctor use `loads`, which leaves the model unset and raises nothing."""

    kind = "no_model"


@dataclass(frozen=True)
class When:
    """A repeat time in local time. weekday is 0 for Monday to 6 for Sunday; only `weekly` uses it."""

    kind: str  # hourly | daily | weekly | monthly | every
    hour: int = 0
    minute: int = 0
    weekday: int = 0
    every: int = 0  # minutes between runs; only `every` uses it
    day: int = 0  # day of the month, 1 to 28; only `monthly` uses it

    def __str__(self) -> str:
        if self.kind == "hourly":
            return "hourly"
        if self.kind == "every":
            return f"every {self.every}m"
        at = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind == "monthly":
            return f"monthly {self.day} {at}"
        return f"daily {at}" if self.kind == "daily" else f"weekly {DAYS[self.weekday]} {at}"


@dataclass(frozen=True)
class SecretUse:
    name: str
    host: str
    purpose: str = "run"


@dataclass(frozen=True)
class Resources:
    """What a run reserves on the host: memory in GB (0 reserves none) and the lane whose slot it takes."""

    ram_gb: float = 0.0
    slot: str = "run"


@dataclass(frozen=True)
class Job:
    name: str
    runner: str
    model: str | None  # None only from `loads` of a job that names none; `load` fills it or refuses
    prompt: str
    cwd: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_turns: int | None = None
    bare: bool = True
    allow_unpriced: bool = False
    green: str | None = None
    sources: tuple[str, ...] = ()
    secrets: tuple[SecretUse, ...] = ()
    runner_options: dict = field(default_factory=dict)
    schedule: When | None = None
    resources: Resources = field(default_factory=Resources)
    preemptible: bool = False
    grace_s: float | None = None
    checkpoint: str | None = None
    deadline: datetime | None = None  # UTC
    purpose: str | None = None
    budget_usd: float | None = None
    strict: bool = False
    parent_run: str | None = None
    on_failure: str | None = None
    log: str | None = None
    uninstall: str | None = None
    expected_rate: str | None = None
    data_class: str | None = None
    coalesce_if_active: bool = True
    skip_missed_s: float | None = None
    max_budget_usd: float | None = None  # not a file key: admission sets it from the reservation


@dataclass(frozen=True)
class Scheduled:
    """A job file with a schedule: `argv` is what follows `ws` on the command line. `timeout_s` is the runtime limit
    the unit enforces (None when the file gives none: the unit then has no limit, as before), `expected_rate_s` the gap
    the dead-man check will expect (None when the file gives none; nothing reads it yet)."""

    name: str
    when: When
    argv: tuple[str, ...]
    timeout_s: float | None = None
    expected_rate_s: float | None = None
    log: str = "file"

    @property
    def records(self) -> bool:
        """True for a job with a runner: its runs leave run records under its name (a ws command's do not)."""
        return self.argv[:2] == ("queue", "tick") or self.argv[:1] == ("run",)


def jobs_dir(env: dict | None = None) -> Path:
    return paths.config_dir(env) / "jobs"


def _string(d: dict, key: str, where: str, required: bool = False) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            raise JobError(f"{where}: {key} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise JobError(f"{where}: {key} must be a non-empty string")
    return v


def _number(d: dict, key: str, where: str, default, integer: bool = False):
    v = d.get(key, default)
    if v is None:
        return None
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok or not v > 0:
        raise JobError(f"{where}: {key} must be a {'whole ' if integer else ''}number above 0")
    return v


def parse_schedule(text: str, where: str) -> When:
    """`hourly`, `daily HH:MM`, `weekly DAY HH:MM`, `monthly D HH:MM` (D from 1 to 28) or `every Nm` (N from 5 to 30).
    Raises JobError naming the key."""
    words = text.lower().split()
    if words == ["hourly"]:
        return When("hourly")
    if len(words) == 2 and words[0] == "every":
        m = _EVERY.match(words[1])
        if not m or not EVERY_MIN <= int(m[1]) <= EVERY_MAX:
            raise JobError(
                f"{where}: every must be a whole number of minutes from {EVERY_MIN} to {EVERY_MAX}, as in every 10m"
            )
        return When("every", every=int(m[1]))
    clock = _CLOCK.match(words[-1]) if words else None
    if clock and len(words) == 2 and words[0] == "daily":
        return When("daily", int(clock[1]), int(clock[2]))
    if clock and len(words) == 3 and words[0] == "weekly" and words[1] in (*DAYS, *_DAY_NAMES):
        return When("weekly", int(clock[1]), int(clock[2]), DAYS.index(words[1][:3]))
    if clock and len(words) == 3 and words[0] == "monthly":
        if not (words[1].isdigit() and 1 <= int(words[1]) <= MONTH_DAY_MAX):
            raise JobError(
                f"{where}: monthly needs a day from 1 to {MONTH_DAY_MAX} (a day every month has), as in monthly 1 04:00"
            )
        return When("monthly", int(clock[1]), int(clock[2]), day=int(words[1]))
    raise JobError(
        f"{where}: schedule must be hourly, daily HH:MM, weekly DAY HH:MM, monthly D HH:MM or every Nm "
        "(for example weekly mon 09:00)"
    )


def latest_slot(when: When, now: datetime, tz=None) -> datetime:
    """The latest time of the schedule at or before `now`, in UTC: the slot a timer's fire is for. A timer that fires
    late (a catch-up after the machine was off, or asleep) still gets the slot it was for, not the minute it ran in.
    The schedule is wall-clock time in `tz` (default this machine's zone, read through the C library as systemd and
    launchd read it). Slots are compared as instants, never as wall clocks, so a daylight-saving change cannot move
    one: in the hour the clocks go back, `hourly` and `every` get a slot for each pass (each is a real hour), while a
    `daily`, `weekly` or `monthly` time that happens twice is one slot, its first; a time the clocks skip is no slot
    that day, so the one before stands."""
    local = now.astimezone(tz).replace(tzinfo=None)  # the wall clock
    if when.kind in ("hourly", "every"):
        wall = local.replace(second=0, microsecond=0)
        wall = wall.replace(minute=0 if when.kind == "hourly" else wall.minute - wall.minute % when.every)
        ok = [t for t in _instants(wall, tz) if t <= now and _shows(t, wall, tz)]
        return max(ok) if ok else min(_instants(wall, tz))
    wall = local.replace(hour=when.hour, minute=when.minute, second=0, microsecond=0)
    if when.kind == "weekly":
        wall -= timedelta(days=(wall.weekday() - when.weekday) % 7)
    elif when.kind == "monthly":
        wall = wall.replace(day=when.day)
    for _ in range(3):  # this period's slot, else the one before (it was later than now, or the clocks skipped it)
        first = _first_instant(wall, tz)
        if first is not None and first <= now:
            return first
        if when.kind == "monthly":
            wall = wall.replace(year=wall.year - (wall.month == 1), month=wall.month - 1 or 12)
        else:
            wall -= timedelta(days=1 if when.kind == "daily" else 7)
    return min(_instants(wall, tz))  # not reached for a real zone: two periods running cannot both be skipped


def _instants(wall: datetime, tz) -> set[datetime]:
    """The UTC readings of a wall-clock time in `tz` (None: this machine's zone, read by the C library, which keeps the
    fold as a ZoneInfo does), one per fold: two in the hour the clocks go back; for a time the clocks skip, readings
    that are not that time at all (`_shows` drops them)."""
    return {
        (wall.replace(fold=f).astimezone() if tz is None else wall.replace(tzinfo=tz, fold=f)).astimezone(UTC)
        for f in (0, 1)
    }


def _shows(t: datetime, wall: datetime, tz) -> bool:
    """True when the clocks in `tz` show `wall` at instant `t`."""
    return t.astimezone(tz).replace(tzinfo=None) == wall.replace(fold=0)


def _first_instant(wall: datetime, tz) -> datetime | None:
    """The first instant the clocks show `wall`; None when they skip it."""
    real = [t for t in _instants(wall, tz) if _shows(t, wall, tz)]
    return min(real) if real else None


def parse_rate(text: str, where: str) -> float:
    """`expected_rate`: a whole number and m, h or d (30m, 6h, 1d). Seconds. Raises JobError naming the key."""
    m = _RATE.match(text.strip().lower())
    if not m or int(m[1]) < 1:
        raise JobError(f"{where}: expected_rate must be a whole number of m, h or d, as in 30m, 6h or 1d")
    return int(m[1]) * _RATE_UNITS[m[2]]


def contract_problems(d: dict, where: str) -> list[str]:
    """What a job file with a schedule lacks of the run contract (the six lines; for a runner job also max_turns and
    budget_usd). Empty when it meets it. Each message names the key."""
    out = [f"{where}: {k} is required by the run contract" for k in CONTRACT_KEYS if k not in d]
    for key, allowed in (("on_failure", ON_FAILURE), ("log", LOG_KINDS)):
        if key in d and d[key] not in allowed:
            out.append(f"{where}: {key} must be one of {', '.join(allowed)}")
    for key in ("uninstall", "data_class"):
        if key in d and not (isinstance(d[key], str) and d[key].strip()):
            out.append(f"{where}: {key} must be a non-empty string")
    if "data_class" in d and isinstance(d["data_class"], str) and not NAME.match(d["data_class"]):
        out.append(f"{where}: data_class must be a plain name (letters, digits, - and _)")
    if "timeout_s" in d and not (
        isinstance(d["timeout_s"], (int, float)) and not isinstance(d["timeout_s"], bool) and d["timeout_s"] > 0
    ):
        out.append(f"{where}: timeout_s must be a number above 0")
    if "expected_rate" in d:
        try:
            parse_rate(str(d["expected_rate"]) if isinstance(d["expected_rate"], str) else "", where)
        except JobError as e:
            out.append(str(e))
    if d.get("command") == "script":
        try:
            script_of(d, where)
        except JobError as e:
            out.append(str(e))
    if "command" not in d:
        out += [
            f"{where}: {k} is required for a job with a runner (its turn limit and spend cap)"
            for k in ("max_turns", "budget_usd")
            if k not in d
        ]
    return out


def _command(d: dict, where: str) -> tuple[str, ...] | None:
    c = _string(d, "command", where)
    for key, needs in COMMAND_ONLY.items():
        if key in d and c != needs:
            raise JobError(f"{where}: {key} goes only in a job with command = {needs!r}")
    if c is None:
        return None
    if c not in COMMANDS:
        raise JobError(f"{where}: command {c!r} is not known (known: {', '.join(COMMANDS)})")
    return COMMANDS[c]


def _home_path(v, key: str, where: str, env: dict | None = None) -> Path:
    """An absolute path, or one under ~ (the home folder of `env`, default this process's). Raises JobError naming
    `key`."""
    if not isinstance(v, str) or not v.strip() or "\0" in v:
        raise JobError(f"{where}: {key} must be a non-empty path")
    if v.startswith("~/") or v == "~":
        home = (env or os.environ).get("HOME") or str(Path.home())
        v = home + v[1:]
    if not os.path.isabs(v):
        raise JobError(f"{where}: {key} must be an absolute path or start with ~/, not {v!r}")
    return Path(os.path.normpath(v))


def script_of(d: dict, where: str, env: dict | None = None) -> tuple[Path, str]:
    """(the script's path, the sha256 it must hash to) of a `command = "script"` job. Raises JobError naming the key."""
    if "script" not in d:
        raise JobError(f"{where}: script is required for a job with command = 'script'")
    path = _home_path(d["script"], "script", where, env)
    sha = d.get("script_sha256")
    if not (isinstance(sha, str) and _SHA256.match(sha)):
        raise JobError(f"{where}: script_sha256 must be the script's sha256, 64 lowercase hex digits (sha256sum)")
    return path, sha


def extra_sources(d: dict, where: str, env: dict | None = None) -> list[Path]:
    """`extra_sources` of a `command = "backup"` job: absolute paths (or under ~). Raises JobError naming the key."""
    v = d.get("extra_sources", [])
    if not isinstance(v, list):
        raise JobError(f"{where}: extra_sources must be a list of paths")
    return [_home_path(x, "extra_sources", where, env) for x in v]


def command_job(name: str, env: dict | None = None) -> dict:
    """The table of the `command =` job `name`, read the way the timers read it (yours, closed to other writers, its pin
    held, its command keys valid). Raises JobError naming what is wrong; FileNotFoundError when there is no such file."""
    folder = jobs_dir(env)
    path = folder / f"{name}.toml"
    if not NAME.match(name):
        raise JobError(f"job {name}: a job name is letters, digits, - and _")
    if not path.is_file():
        raise FileNotFoundError(path)
    check_private(folder)
    check_private(path)
    try:
        raw = path.read_bytes()
        d = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise JobError(f"{path}: {e}") from e
    check_pin(folder, name, raw)
    if _command(d, str(path)) is None:
        raise JobError(f"{path}: this job has a runner, not a command")
    return d


def loads(name: str, text: str, base: Path, where: str | None = None) -> Job:
    """Parse and check a job file's text. `base` is the folder `prompt_file` is relative to. Pure: the profile is not
    read, so a job that names no model comes back with `model` None (`load` resolves it)."""
    where = where or f"job {name}"
    if not NAME.match(name):
        raise JobError(f"{where}: a job name is letters, digits, - and _")
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise JobError(f"{where}: {e}") from e
    unknown = sorted(set(d) - KEYS)
    if unknown:
        raise JobError(f"{where}: unknown key {unknown[0]!r} (known: {', '.join(sorted(KEYS))})")
    if _command(d, where):
        raise JobError(f"{where}: this job runs a ws command on a schedule; `ws run` is for jobs with a runner")
    schedule = _string(d, "schedule", where)
    runner = _string(d, "runner", where, required=True)
    known = adapters.all_adapters()
    if runner not in known:
        raise JobError(f"{where}: runner {runner!r} is not known (known: {', '.join(known)})")
    model = _string(d, "model", where)
    prompt, prompt_file = _string(d, "prompt", where), _string(d, "prompt_file", where)
    if (prompt is None) == (prompt_file is None):
        raise JobError(f"{where}: give exactly one of prompt and prompt_file")
    if prompt_file is not None:
        f = (base / prompt_file).resolve()
        if not f.is_relative_to(base.resolve()):
            raise JobError(f"{where}: prompt_file {prompt_file!r} must be inside {base}")
        try:
            check_private(f)
            prompt = f.read_text(encoding="utf-8")
        except JobError as e:
            raise JobError(f"{where}: prompt_file: {e}") from e
        except (OSError, UnicodeDecodeError) as e:
            raise JobError(f"{where}: prompt_file {str(f)!r} cannot be read ({type(e).__name__})") from e
        if not prompt.strip():
            raise JobError(f"{where}: prompt_file {str(f)!r} is empty")
    bare = d.get("bare", True)
    if not isinstance(bare, bool):
        raise JobError(f"{where}: bare must be true or false")
    allow_unpriced = d.get("allow_unpriced", False)
    if not isinstance(allow_unpriced, bool):
        raise JobError(f"{where}: allow_unpriced must be true or false")
    green = _string(d, "green", where)
    if green is not None and green not in GREEN_LEVELS:
        raise JobError(f"{where}: green must be one of {', '.join(GREEN_LEVELS)}")
    sources = d.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise JobError(f"{where}: sources must be a list of source names")
    secrets_raw = d.get("secrets", [])
    if not isinstance(secrets_raw, list) or not all(isinstance(s, dict) for s in secrets_raw):
        raise JobError(f"{where}: secrets must be [[secrets]] tables")
    secrets = []
    for i, s in enumerate(secrets_raw, 1):
        w = f"{where}: secrets #{i}"
        extra = sorted(set(s) - {"name", "host", "purpose"})
        if extra:
            raise JobError(f"{w}: unknown key {extra[0]!r}")
        sname, host = _string(s, "name", w, required=True), _string(s, "host", w, required=True)
        if not NAME_RE.match(sname):
            raise JobError(f"{w}: name {sname!r} is not a valid secret name")
        if not HOST_RE.match(host.lower()):
            raise JobError(f"{w}: host {host!r} is not a host name (no scheme, no path)")
        secrets.append(SecretUse(sname, host.lower(), _string(s, "purpose", w) or "run"))
    options = d.get("runner_options", {})
    if not isinstance(options, dict):
        raise JobError(f"{where}: runner_options must be a table")
    preemptible = d.get("preemptible", False)
    if not isinstance(preemptible, bool):
        raise JobError(f"{where}: preemptible must be true or false")
    strict = d.get("strict", False)
    if not isinstance(strict, bool):
        raise JobError(f"{where}: strict must be true or false")
    purpose = _string(d, "purpose", where)
    if purpose is not None and not NAME.match(purpose):
        raise JobError(f"{where}: purpose {purpose!r} is not a plain name (letters, digits, - and _)")
    parent_run = _string(d, "parent_run", where)
    if parent_run is not None and not NAME.match(parent_run):
        raise JobError(f"{where}: parent_run {parent_run!r} is not a run id (letters, digits, - and _)")
    coalesce = d.get("coalesce_if_active", True)
    if not isinstance(coalesce, bool):
        raise JobError(f"{where}: coalesce_if_active must be true or false")
    skip = d.get("skip_missed")
    if skip is not None and (
        isinstance(skip, bool) or not isinstance(skip, (int, float)) or not 0 <= skip < float("inf")
    ):
        raise JobError(f"{where}: skip_missed must be a number of seconds, 0 or more")
    return Job(
        name=name,
        runner=runner,
        model=model,
        prompt=prompt,
        cwd=_string(d, "cwd", where),
        timeout_s=_number(d, "timeout_s", where, DEFAULT_TIMEOUT_S),
        max_turns=_number(d, "max_turns", where, None, integer=True),
        bare=bare,
        allow_unpriced=allow_unpriced,
        green=green,
        sources=tuple(sources),
        secrets=tuple(secrets),
        runner_options=options,
        schedule=parse_schedule(schedule, f"{where}: schedule") if schedule else None,
        resources=_resources(d.get("resources"), where),
        preemptible=preemptible,
        grace_s=_number(d, "grace_s", where, None),
        checkpoint=_checkpoint(d, where),
        deadline=parse_deadline(d.get("deadline"), where),
        purpose=purpose,
        budget_usd=_number(d, "budget_usd", where, None),
        strict=strict,
        parent_run=parent_run,
        on_failure=_string(d, "on_failure", where),
        log=_string(d, "log", where),
        uninstall=_string(d, "uninstall", where),
        expected_rate=_string(d, "expected_rate", where),
        data_class=_string(d, "data_class", where),
        coalesce_if_active=coalesce,
        skip_missed_s=float(skip) if skip is not None else None,
    )


def _resources(v, where: str) -> Resources:
    """`[resources]`: ram_gb (a number above 0) and slot (a plain name). Raises JobError naming the key."""
    if v is None:
        return Resources()
    if not isinstance(v, dict):
        raise JobError(f"{where}: resources must be a table")
    extra = sorted(set(v) - RESOURCE_KEYS)
    if extra:
        raise JobError(
            f"{where}: resources has an unknown key {extra[0]!r} (known: {', '.join(sorted(RESOURCE_KEYS))})"
        )
    ram = _number(v, "ram_gb", f"{where}: resources", 0.0) if "ram_gb" in v else 0.0
    slot = _string(v, "slot", f"{where}: resources") or "run"
    if not NAME.match(slot):
        raise JobError(f"{where}: resources slot {slot!r} is not a plain name (letters, digits, - and _)")
    return Resources(float(ram), slot)


def _checkpoint(d: dict, where: str) -> str | None:
    c = _string(d, "checkpoint", where)
    if c is not None and (os.path.isabs(c) or ".." in Path(c).parts):
        raise JobError(f"{where}: checkpoint must be a file name inside the job's cwd, not {c!r}")
    return c


def parse_deadline(v, where: str) -> datetime | None:
    """`deadline`: a TOML time with an offset, or text such as 2026-11-10T09:00:00Z. Always UTC; a time with no zone is
    refused, because a deadline read in the wrong zone starts or stops a run hours off."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip())
        except ValueError:
            v = None
    if not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None:
        raise JobError(f"{where}: deadline must be a UTC time with a zone, such as 2026-11-10T09:00:00Z")
    return v.astimezone(UTC)


def check_private(path: Path) -> None:
    """Raises JobError unless `path` belongs to this user and nobody else can write to it."""
    try:
        st = path.stat()
    except OSError as e:
        raise JobError(f"{path}: {e.strerror or type(e).__name__}") from e
    if st.st_uid != os.geteuid():
        raise JobError(f"{path} belongs to another user; ws will not run what someone else could have written")
    if st.st_mode & 0o022:
        raise JobError(f"{path} can be written by others (mode {st.st_mode & 0o777:04o}); run `chmod go-w {path}`")


PINS = ".pins.json"
PINS_SCHEMA = 1


def digest(text: bytes, prompt: tuple[str, bytes] | None = None) -> str:
    """The sha256 a job is pinned to: its file's bytes and, if it has one, its prompt_file's name and bytes."""
    h = hashlib.sha256(b"ws-job-pin-1\0" + len(text).to_bytes(8, "big") + text)
    if prompt is not None:
        name = prompt[0].encode("utf-8")
        h.update(len(name).to_bytes(8, "big") + name + len(prompt[1]).to_bytes(8, "big") + prompt[1])
    return h.hexdigest()


def read_pins(folder: Path) -> dict[str, dict]:
    """name -> pin record from `.pins.json`. No file is no pins; a file that is not ours, is open to writers or does not
    parse raises JobError, so a damaged pin file refuses every job instead of letting a changed one through."""
    path = folder / PINS
    if not path.exists() and not path.is_symlink():
        return {}
    check_private(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise JobError(f"{path} cannot be read ({type(e).__name__}); every job is refused until it is fixed") from e
    jobs_ = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs_, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("sha256"), str) for k, v in jobs_.items()
    ):
        raise JobError(f"{path} is not a pins file; every job is refused until it is fixed")
    return jobs_


def file_digest(folder: Path, name: str, text: bytes | None = None) -> str:
    """The digest of the job `name` as it is on disk now (its file and its prompt_file, if it names one)."""
    path = folder / f"{name}.toml"
    raw = path.read_bytes() if text is None else text
    try:
        d = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return digest(raw)
    pf = d.get("prompt_file")
    if not isinstance(pf, str):
        return digest(raw)
    try:
        return digest(raw, (pf, (folder / pf).read_bytes()))
    except OSError:
        return digest(raw, (pf, b""))


def check_pin(folder: Path, name: str, text: bytes | None = None) -> None:
    """Raises JobError when `name` is pinned and its files no longer hash to the pin."""
    pin = read_pins(folder).get(name)
    if pin is None:
        return
    if file_digest(folder, name, text) != pin["sha256"]:
        raise JobError(
            f"job {name} was changed after `ws job add` pinned it; ws will not run a job nobody reviewed. "
            f"Put the change in with `ws job add PATH --replace`, or `ws job remove {name}`"
        )


def load(name: str, env: dict | None = None) -> Job:
    """The job `name` from the jobs folder. The folder and the file must be yours and closed to other writers."""
    folder = jobs_dir(env)
    path = folder / f"{name}.toml"
    if not NAME.match(name):
        raise JobError(f"job {name}: a job name is letters, digits, - and _")
    if not path.is_file():
        raise JobError(f"no job {name!r}: expected {path}")
    check_private(folder)
    check_private(path)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise JobError(f"{path}: {e}") from e
    check_pin(folder, name, raw)
    job = loads(name, text, folder, where=str(path))
    if job.model is not None:
        return job  # the job names its model: the profile is not read, as before [models] existed
    try:
        prof = profile.load(profile.profile_path(env))
    except profile.ProfileError as e:
        raise JobError(f"job {name}: names no model and the profile cannot be read: {e}") from e
    model, _ = resolve_model(job, prof)
    if model is None:
        raise NoModelError(no_model_message(job))
    return replace(job, model=model)


MODEL_SOURCES = ("job", "purpose", "default", "none")


def models_table(prof: dict | None) -> dict[str, str]:
    """The profile's [models] table: purpose name (or `default`) -> model id. No profile or no table is empty. A table
    with any bad entry raises JobError naming the key: it is never used in part."""
    raw = (prof or {}).get("models")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise JobError('profile: [models] must be a table of purpose = "model id"')
    for k, v in raw.items():
        if not NAME.match(k):
            raise JobError(f"profile: [models] key {k!r} is not a plain name (letters, digits, - and _)")
        if not isinstance(v, str) or not v.strip():
            raise JobError(f"profile: [models].{k} must be a non-empty model id")
    return dict(raw)


def resolve_model(job: Job, prof: dict | None) -> tuple[str | None, str]:
    """(model id, where it came from): the job's own `model`, else [models].<purpose>, else [models].default, else
    (None, "none"). Ids pass through unchanged; the run's provider, class and pricing checks judge them after."""
    if job.model is not None:
        return job.model, "job"
    table = models_table(prof)
    if job.purpose is not None and job.purpose in table:
        return table[job.purpose], "purpose"
    if "default" in table:
        return table["default"], "default"
    return None, "none"


def no_model_message(job: Job) -> str:
    how = f"ws config model {job.purpose} ID" if job.purpose else "ws config model ID"
    return f"no model for job {job.name}: set model in the job file, or run: {how}"


def _scan(env: dict | None) -> tuple[dict[str, str], dict[str, str]]:
    """(load problems, contract gaps) by job name. A job with a load problem has no gap entry: it is refused first."""
    folder = jobs_dir(env)
    if not folder.is_dir():
        return {}, {}
    try:
        check_private(folder)
    except JobError as e:
        return {"(folder)": str(e)}, {}
    bad: dict[str, str] = {}
    gaps: dict[str, str] = {}
    for path in sorted(folder.glob("*.toml")):
        if not NAME.match(path.stem):
            continue
        try:
            check_private(path)
            if path.stem in RESERVED:
                raise JobError(f"{path}: the name {path.stem!r} is reserved for a unit ws writes itself")
            raw = path.read_bytes()
            check_pin(folder, path.stem, raw)
            text = raw.decode("utf-8")
            d = tomllib.loads(text)
            if _command(d, str(path)):  # a schedule-only job: `ws run` refuses it, the timer needs a good schedule
                parse_schedule(_string(d, "schedule", str(path)) or "", f"{path}: schedule")
            else:
                loads(path.stem, text, folder, str(path))
            if "schedule" in d and (lacks := contract_problems(d, str(path))):
                gaps[path.stem] = "; ".join(lacks)
        except (JobError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            bad[path.stem] = str(e)
    return bad, gaps


def problems(env: dict | None = None, contract: bool = False) -> dict[str, str]:
    """name -> what is wrong, for each job file that `ws run` would refuse. A bad job never stops the others: this
    lists it (doctor does), and `load` still gives every good job. An unusable jobs folder is one entry, "(folder)".
    With `contract=True` the run contract's gaps (see `contract_gaps`) are listed too, as problems."""
    bad, gaps = _scan(env)
    return {**gaps, **bad} if contract else bad


def contract_gaps(env: dict | None = None) -> dict[str, str]:
    """name -> what a scheduled job file that loads lacks of the run contract. `ws run` and the timer still run it, so
    `ws config` and `ws doctor` show these as warnings, not failures."""
    return _scan(env)[1]


def _contract_values(d: dict, where: str) -> tuple[float | None, float | None]:
    """(timeout_s, expected_rate in seconds) for the timer. A value that does not parse is the contract check's business
    (`problems`), not a reason to stop every timer: it is None, as if the file did not give it."""
    try:
        timeout = _number(d, "timeout_s", where, None)
    except JobError:
        timeout = None
    try:
        rate = d.get("expected_rate")
        rate_s = parse_rate(rate, f"{where}: expected_rate") if isinstance(rate, str) else None
    except JobError:
        rate_s = None
    return timeout, rate_s


def scheduled(env: dict | None = None) -> list[Scheduled]:
    """Every job file with a `schedule`, checked the way `ws run` checks it. A missing jobs folder is no jobs; a bad
    file raises JobError (a timer must never run what `ws run` would refuse)."""
    folder = jobs_dir(env)
    if not folder.is_dir():
        return []
    check_private(folder)
    out = []
    for path in sorted(folder.glob("*.toml")):
        if not NAME.match(path.stem):
            continue
        check_private(path)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            d = tomllib.loads(text)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise JobError(f"{path}: {e}") from e
        check_pin(folder, path.stem, raw)
        if "schedule" not in d:
            continue
        if path.stem in RESERVED:
            raise JobError(f"{path}: the name {path.stem!r} is reserved for a unit ws writes itself")
        where = str(path)
        command = _command(d, where)
        if command is None:
            job = loads(path.stem, text, folder, where)  # a runner job must be valid before it gets a timer
            if credential_kind(job) == "subscription":  # the queue never runs one: it keeps `ws run` as before
                command = ("run", path.stem)
        elif command == COMMANDS["script"]:
            script_of(d, where, env)
            command = (*command, path.stem)
        elif command == COMMANDS["backup"]:
            extra_sources(d, where, env)
        when = parse_schedule(_string(d, "schedule", where) or "", f"{where}: schedule")
        timeout_s, rate_s = _contract_values(d, where)
        out.append(
            Scheduled(
                path.stem,
                when,
                command or _tick(path.stem),
                timeout_s=timeout_s,
                expected_rate_s=rate_s,
                log=d.get("log") if d.get("log") in LOG_KINDS else "file",
            )
        )
    # a tick unit drains the whole queue, so it may be running another job's run when its own limit comes: each gets
    # the longest limit of them (no limit if one has none), so systemd never kills a drain in the middle of a run
    ticks = [s for s in out if s.argv[:2] == ("queue", "tick")]
    limits = [s.timeout_s for s in ticks]
    longest = None if None in limits or not limits else max(limits)
    return [replace(s, timeout_s=longest) if s in ticks else s for s in out]


def credential_kind(job: Job) -> str:
    """`api_key` when the job leases a key through `[[secrets]]`, `local` when its provider's base URL is a local model
    server (the same check `ws job add` and `ws run` make: ws.adapters.opencode.base_url_problem, so this machine, the
    local network or a tailnet), else `subscription`: the runner would use whatever login it is signed in with."""
    if job.secrets:
        return "api_key"
    provider = job.runner_options.get("provider") if isinstance(job.runner_options.get("provider"), dict) else {}
    options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
    base = options.get("baseURL")
    if isinstance(base, str):
        from ws.adapters import opencode  # late: the adapter imports this module

        if opencode.base_url_problem(base) is None:
            return "local"
    return "subscription"


def _tick(name: str) -> tuple[str, ...]:  # what a runner job's timer runs
    return ("queue", "tick", name, "--slot", "schedule", "--run")


def check_options(job: Job, allowed: set[str]) -> None:
    """Raises ValueError naming the first runner_options key the runner does not know."""
    for k in sorted(set(job.runner_options) - allowed):
        raise ValueError(f"runner_options.{k} is not an option of {job.runner} (known: {', '.join(sorted(allowed))})")


def option(job: Job, key: str, kind: type, default=None):
    """One runner_options value, checked to be a `kind`. Raises ValueError naming runner_options.<key>."""
    v = job.runner_options.get(key, default)
    if v is default:
        return v
    if not isinstance(v, kind) or isinstance(v, bool) and kind is not bool:
        raise ValueError(f"runner_options.{key} must be a {kind.__name__}")
    return v


def string_list(job: Job, key: str) -> list[str]:
    v = option(job, key, list, [])
    if not all(isinstance(x, str) and x for x in v):
        raise ValueError(f"runner_options.{key} must be a list of strings")
    return v


# Names a job's env may not set: they would point the runner away from the broker or the gate, hand it a credential,
# or load code into it. The adapter sets the ones it needs after the job's env, so the job cannot get in the way.
_RESERVED_PREFIXES = ("ANTHROPIC_", "OPENAI_", "OPENCODE_", "CLAUDE_", "WS_", "LD_", "DYLD_", "PYTHON")
_RESERVED_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")
_RESERVED_NAMES = {"PATH", "HOME", "NODE_OPTIONS", "BASH_ENV", "ENV", "SHELL"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def extra_env(job: Job) -> dict[str, str]:
    """runner_options.env: variables the job asks the runner to start with (plain text in the file, so never a
    secret). Raises ValueError naming runner_options.env for a name that is reserved."""
    v = option(job, "env", dict, {})
    if not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        raise ValueError("runner_options.env must map names to strings")
    for k in v:
        up = k.upper()
        if not _ENV_NAME.match(k):
            raise ValueError(f"runner_options.env: {k!r} is not a variable name")
        if up in _RESERVED_NAMES or up.startswith(_RESERVED_PREFIXES) or up.endswith(_RESERVED_SUFFIXES):
            raise ValueError(
                f"runner_options.env: {k} is reserved (ws sets the runner's credentials, paths and config)"
            )
    return dict(v)
