"""Job files: `jobs/<name>.toml` in ws's config folder, one headless job each, run with `ws run <name>`.

    runner = "claude-code"              adapter name (see `ws gate generate --help`)
    model = "claude-haiku-4-5"          the smallest model that passes
    prompt = "..."                      or prompt_file = "daily.md" (relative to the jobs folder)
    cwd = "~/work/thing"                default: an empty folder made for the run
    timeout_s = 900
    max_turns = 12                      optional
    bare = true                         headless default: no hooks, plugins or auto-discovered context
    allow_unpriced = false              run even if the runner cannot price the model (counted at your fallback)
    green = "targeted"                  optional: what the job's verification covers (targeted | package | full |
                                        merge_ready); written to the run record as `green`
    sources = ["notes"]                 policy sources the job reads (the Rule of Two looks at these)
    schedule = "daily 03:30"            optional: hourly | daily HH:MM | weekly DAY HH:MM | every Nm (N from 5 to 30),
                                        in local time. `ws schedule apply` turns it into a systemd timer or a launchd
                                        agent.
    command = "backup"                  instead of runner, model and prompt: a ws command the schedule runs
                                        (`ws backup run`, `ws queue run`). Only `ws schedule` runs these; `ws run`
                                        refuses them.

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

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import adapters, paths
from ws.secrets import HOST_RE, NAME_RE

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
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
}  # the ws commands a job file may schedule in place of a runner
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_EVERY = re.compile(r"^(\d{1,2})m$")
EVERY_MIN, EVERY_MAX = 5, 30  # `every Nm`: minutes between runs
RESOURCE_KEYS = {"ram_gb", "slot"}


class JobError(Exception):
    pass


@dataclass(frozen=True)
class When:
    """A repeat time in local time. weekday is 0 for Monday to 6 for Sunday; only `weekly` uses it."""

    kind: str  # hourly | daily | weekly | every
    hour: int = 0
    minute: int = 0
    weekday: int = 0
    every: int = 0  # minutes between runs; only `every` uses it

    def __str__(self) -> str:
        if self.kind == "hourly":
            return "hourly"
        if self.kind == "every":
            return f"every {self.every}m"
        at = f"{self.hour:02d}:{self.minute:02d}"
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
    model: str
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
    """`hourly`, `daily HH:MM`, `weekly DAY HH:MM` or `every Nm` (N from 5 to 30). Raises JobError naming the key."""
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
    raise JobError(
        f"{where}: schedule must be hourly, daily HH:MM, weekly DAY HH:MM or every Nm (for example weekly mon 09:00)"
    )


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
    if "command" not in d:
        out += [
            f"{where}: {k} is required for a job with a runner (its turn limit and spend cap)"
            for k in ("max_turns", "budget_usd")
            if k not in d
        ]
    return out


def _command(d: dict, where: str) -> tuple[str, ...] | None:
    c = _string(d, "command", where)
    if c is None:
        return None
    if c not in COMMANDS:
        raise JobError(f"{where}: command {c!r} is not known (known: {', '.join(COMMANDS)})")
    return COMMANDS[c]


def loads(name: str, text: str, base: Path, where: str | None = None) -> Job:
    """Parse and check a job file's text. `base` is the folder `prompt_file` is relative to."""
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
    model = _string(d, "model", where, required=True)
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
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise JobError(f"{path}: {e}") from e
    return loads(name, text, folder, where=str(path))


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
            text = path.read_text(encoding="utf-8")
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
            text = path.read_text(encoding="utf-8")
            d = tomllib.loads(text)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise JobError(f"{path}: {e}") from e
        if "schedule" not in d:
            continue
        where = str(path)
        command = _command(d, where)
        if command is None:
            loads(path.stem, text, folder, where)  # a runner job must be valid before it gets a timer
        when = parse_schedule(_string(d, "schedule", where) or "", f"{where}: schedule")
        timeout_s, rate_s = _contract_values(d, where)
        out.append(
            Scheduled(
                path.stem,
                when,
                command or ("run", path.stem),
                timeout_s=timeout_s,
                expected_rate_s=rate_s,
                log=d.get("log") if d.get("log") in LOG_KINDS else "file",
            )
        )
    return out


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
