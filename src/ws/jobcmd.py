"""`ws job add` and `ws job remove`: the one door a job file comes in by, checked, copied, pinned and logged.

    ws job add PATH [--replace] [--no-apply]   check the job, copy it (and its prompt_file) into the jobs folder, pin it
    ws job remove NAME                         stop its timer, delete what `add` copied, drop the pin

`add` COPIES, never links: a link would let whoever can write the source change a live job unreviewed. The copy's
sha256 (ws.jobs.digest) goes in `.pins.json`, and `ws run` and the timers refuse a pinned job whose files differ. The
job must load as `ws run` loads it (ws.jobs.loads) and pass the install checks here, each named in its refusal:

- every tool in runner_options.allowed_tools is a tool the policy's [gate] allow_mcp lets through, or a built-in tool
  that cannot write or run commands (Write, Edit, MultiEdit, NotebookEdit and Bash are refused, with or without a
  scope such as `Bash(git *)`);
- every [[secrets]] name is in the secrets registry, at one of its hosts and for one of its purposes;
- runner_options has no env, exe or provider (each can point the runner at other code or another endpoint; a job that
  needs one is written by hand);
- a `command =` job names one of the commands ws.jobs.COMMANDS allows;
- budget_usd is at most the profile's monthly_cap_usd, and the purpose's cap when it names a purpose; with no
  monthly cap set, a runner job is refused (as `ws run` would refuse it).

Replacing an installed job needs `--replace`, and shows the diff first. A job that adds power the installed jobs lack (a
secret, a tool, a schedule, a command, or a higher budget; on a replace, compared with the job it replaces) needs a
person's yes when it is added from inside an agent session: the refusal carries a `ws approve` id bound to the job's
sha256, and the identical `ws job add` then goes through once. A same-power fix needs no approval. From a person's own
terminal no approval is asked. Every add, replace and remove writes one line to `<state>/logs/jobs-installed.jsonl`
(the name, the sha256, what widened, never the prompt). After an add, `ws schedule apply` runs (it is idempotent over
every job) unless `--no-apply`.

Sessions still cannot write the jobs folder directly (the gate protects the config folder); this command is the door.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fnmatch
import json
import os
import re
import secrets as pysecrets
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import agentsession, approvals, jobs, jsonl, output, paths, policy
from ws import secrets as wsecrets
from ws.output import WsError

COMMAND = "job"

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"})
REFUSED_OPTIONS = ("env", "exe", "provider")
LOG_NAME = "jobs-installed.jsonl"
_TOOL_BASE = re.compile(r"^([A-Za-z0-9_.*-]+)")


@dataclass
class Power:
    """What a job can reach: secrets (name@host), tools, whether it has a timer, its command and its budget."""

    secrets: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    schedule: str | None = None
    command: str | None = None
    budget: float | None = None


def _power(d: dict) -> Power:
    p = Power()
    for s in d.get("secrets") or []:
        if isinstance(s, dict):
            p.secrets.add(f"{s.get('name')}@{str(s.get('host', '')).lower()} for {s.get('purpose') or 'run'}")
    opts = d.get("runner_options") if isinstance(d.get("runner_options"), dict) else {}
    tools = opts.get("allowed_tools") if isinstance(opts.get("allowed_tools"), list) else []
    p.tools = {t for t in tools if isinstance(t, str)}
    p.schedule = d.get("schedule") if isinstance(d.get("schedule"), str) else None
    p.command = d.get("command") if isinstance(d.get("command"), str) else None
    b = d.get("budget_usd")
    p.budget = float(b) if isinstance(b, (int, float)) and not isinstance(b, bool) else None
    return p


def _against(new: Power, base: Power, schedule_is_new: bool) -> list[str]:
    out = [f"secret {s}" for s in sorted(new.secrets - base.secrets)]
    out += [f"tool {t}" for t in sorted(new.tools - base.tools)]
    if new.schedule is not None and (schedule_is_new or new.schedule != base.schedule):
        out.append(f"schedule {new.schedule}")
    if new.command is not None and new.command != base.command:
        out.append(f"command {new.command}")
    if new.budget is not None and (base.budget is None or new.budget > base.budget):
        out.append(f"budget_usd {new.budget:g}")
    return out


def widening(new: Power, base: Power | None, installed: list[Power]) -> list[str]:
    """What `new` adds. Against `base` (the job it replaces) when there is one. A new job is held against each installed
    job on its own and the closest one counts, so it cannot gather one job's secret and another's tool unasked; its
    timer is always new."""
    if base is not None:
        return _against(new, base, schedule_is_new=False)
    options = [_against(new, p, schedule_is_new=True) for p in installed] or [_against(new, Power(), True)]
    return min(options, key=len)


def _tool_problem(tool: str, allow_mcp: tuple[str, ...]) -> str | None:
    if tool.startswith("mcp__"):
        if any(fnmatch.fnmatchcase(tool, p) for p in allow_mcp):
            return None
        return f"runner_options.allowed_tools: {tool} is not in [gate] allow_mcp of the policy"
    m = _TOOL_BASE.match(tool)
    base = m.group(1) if m else tool
    if base in WRITE_TOOLS or not m:
        return f"runner_options.allowed_tools: {tool} can write files or run commands; a job added this way may not"
    return None


def _monthly_cap(env) -> float | None:
    from ws.profile import ProfileError, load, profile_path

    try:
        prof = load(profile_path(env)) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), "fix the profile, then add the job again") from e
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    cfg = roles.get("budget") if isinstance(roles.get("budget"), dict) else {}
    v = cfg.get("monthly_cap_usd")
    cap = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    return cap


def problems(d: dict, pol: policy.Policy, reg: dict[str, wsecrets.Entry], monthly_cap: float | None) -> list[str]:
    """Every install check the job fails, each naming its key. Empty when it passes."""
    out: list[str] = []
    opts = d.get("runner_options") if isinstance(d.get("runner_options"), dict) else {}
    if isinstance(d.get("runner"), str):
        from ws import adapters

        known = getattr(adapters.all_adapters().get(d["runner"]), "OPTIONS", None)
        for k in sorted(set(opts) - set(known)) if known is not None else ():
            out.append(f"runner_options.{k} is not an option of {d['runner']} (known: {', '.join(sorted(known))})")
    for k in REFUSED_OPTIONS:
        if k in opts:
            out.append(f"runner_options.{k}: a job added with `ws job add` may not set it (write such a job by hand)")
    allow = pol.gate.allow_mcp if pol.gate.allow_mcp is not None else policy.MCP_SHIPPED
    tools = opts.get("allowed_tools", [])
    for t in tools if isinstance(tools, list) else []:
        if isinstance(t, str) and (why := _tool_problem(t, allow)):
            out.append(why)
    for i, s in enumerate(d.get("secrets") or [], 1):
        if not isinstance(s, dict):
            continue
        name, host = s.get("name"), str(s.get("host", "")).lower()
        purpose = s.get("purpose") or "run"
        e = reg.get(name) if isinstance(name, str) else None
        if e is None:
            out.append(f"secrets #{i}: {name!r} is not in the secrets registry")
        elif e.kind == "header" and host not in e.hosts:
            out.append(
                f"secrets #{i}: {name} may not be used at {host} (the registry allows {', '.join(e.hosts) or 'no host'})"
            )
        elif e.purposes and purpose not in e.purposes:
            out.append(
                f"secrets #{i}: {name} may not be used for {purpose!r} (the registry allows {', '.join(e.purposes)})"
            )
    b = d.get("budget_usd")
    runner_job = "command" not in d
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        if monthly_cap is None:
            out.append("budget_usd: no monthly_cap_usd in [roles.budget] of the profile, so no budget can be checked")
        elif b > monthly_cap:
            out.append(f"budget_usd {b:g} is above the profile's monthly_cap_usd {monthly_cap:g}")
        purpose = d.get("purpose")
        pcap = pol.budget_purposes.get(purpose) if isinstance(purpose, str) else None
        if pcap is not None and b > pcap:
            out.append(f"budget_usd {b:g} is above the {purpose} purpose's cap {pcap:g}")
    elif runner_job and monthly_cap is None:
        out.append("no monthly_cap_usd in [roles.budget] of the profile: `ws run` would refuse this job")
    return out


def _installed(folder: Path, skip: str | None) -> list[Power]:
    """The power of every job in the folder that parses (a broken one adds nothing)."""
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.toml")):
        if path.stem == skip or not jobs.NAME.match(path.stem):
            continue
        with contextlib.suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            out.append(_power(tomllib.loads(path.read_text(encoding="utf-8"))))
    return out


def _taken(folder: Path, pins: dict[str, dict], name: str) -> set[str]:
    """The files of every other job: their .toml, the files their pins list and the prompt_file each names."""
    out = {p.name for p in folder.glob("*.toml") if p.stem != name} if folder.is_dir() else set()
    for other, pin in pins.items():
        if other != name and isinstance(pin.get("files"), list):
            out |= {f for f in pin["files"] if isinstance(f, str)}
    for p in folder.glob("*.toml") if folder.is_dir() else ():
        if p.stem == name:
            continue
        with contextlib.suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pf = tomllib.loads(p.read_text(encoding="utf-8")).get("prompt_file")
            if isinstance(pf, str):
                out.add(pf)
    return out | {jobs.PINS}


def _write_private(path: Path, data: bytes) -> None:
    """A new file, mode 0600, then an atomic rename over the old one."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{pysecrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _save_pins(folder: Path, pins: dict[str, dict]) -> None:
    doc = {"schema": jobs.PINS_SCHEMA, "jobs": dict(sorted(pins.items()))}
    _write_private(folder / jobs.PINS, (json.dumps(doc, indent=2) + "\n").encode("utf-8"))


def _log(env, action: str, name: str, sha: str | None, widened: list[str], approved: bool) -> None:
    rec = {
        "schema": 1,
        "type": "ws.job-install/1",
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "action": action,
        "job": name,
        "sha256": sha,
        "widened": widened,
        "approved": approved,
        "in_session": _in_session(env),
    }
    jsonl.append(paths.state_dir(env) / "logs" / LOG_NAME, rec)


def _in_session(env) -> bool:
    return agentsession.refusal(env, sys.stdin.isatty(), sys.stdout.isatty()) is not None


def _diff(old: str, new: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"installed/{label}", f"new/{label}")
    )


def _read_source(src: Path) -> tuple[str, bytes, dict, tuple[str, bytes] | None]:
    """(name, the file's bytes, its table, (prompt_file, bytes) or None). Raises WsError naming what is wrong."""
    name = src.stem
    if src.suffix != ".toml" or not jobs.NAME.match(name):
        raise WsError("usage", f"{src}: a job file is NAME.toml, NAME being letters, digits, - and _", "", 2)
    if name in jobs.RESERVED:
        raise WsError("usage", f"the name {name!r} is reserved for a unit ws writes itself", "rename the file", 2)
    try:
        raw = src.read_bytes()
        d = tomllib.loads(raw.decode("utf-8"))
    except FileNotFoundError as e:
        raise WsError("not_found", f"no job file at {src}", "", 2) from e
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise WsError("bad_input", f"{src}: {e}", "fix the file, then add it again", 2) from e
    prompt = None
    pf = d.get("prompt_file")
    if pf is not None:
        if not isinstance(pf, str) or not pf or Path(pf).name != pf or pf.startswith(".") or pf.endswith(".toml"):
            raise WsError(
                "bad_input",
                f"{src}: prompt_file must be a file name next to the job file, not a .toml or a dot file",
                "",
                2,
            )
        try:
            prompt = (pf, (src.parent / pf).read_bytes())
        except OSError as e:
            raise WsError("bad_input", f"{src}: prompt_file {pf} cannot be read ({type(e).__name__})", "", 2) from e
    return name, raw, d, prompt


def _validate(name: str, raw: bytes, d: dict, src: Path) -> None:
    """The job must load the way `ws run` (or, for a command job, the timer) loads it."""
    where = str(src)
    try:
        if "command" in d:
            jobs._command(d, where)
            unknown = sorted(set(d) - jobs.KEYS)
            if unknown:
                raise jobs.JobError(f"{where}: unknown key {unknown[0]!r}")
            jobs.parse_schedule(jobs._string(d, "schedule", where, required=True) or "", f"{where}: schedule")
        else:
            jobs.loads(name, raw.decode("utf-8"), src.parent, where)
    except jobs.JobError as e:
        raise WsError("bad_input", str(e), "fix the job file, then add it again", 2) from e


def cmd_add(args: argparse.Namespace) -> int:
    env = os.environ
    src = Path(args.path).expanduser()
    name, raw, d, prompt = _read_source(src)
    _validate(name, raw, d, src)
    try:
        pol = policy.load()
        reg = wsecrets.load_registry(wsecrets.registry_path())
    except (policy.PolicyError, wsecrets.SecretsError) as e:
        raise WsError("bad_config", str(e), "fix it, then add the job again") from e
    cap = _monthly_cap(env)
    if bad := problems(d, pol, reg, cap):
        raise WsError(
            "refused", f"job {name} was not added: " + "; ".join(bad), "change the job file, then add it again", 3
        )

    folder = jobs.jobs_dir(env)
    dest = folder / f"{name}.toml"
    sha = jobs.digest(raw, prompt)
    try:
        pins = jobs.read_pins(folder) if folder.is_dir() else {}
    except jobs.JobError as e:
        raise WsError("bad_config", str(e), "fix or remove the pins file, then add the job again") from e
    if prompt is not None and prompt[0] in _taken(folder, pins, name):
        raise WsError(
            "refused", f"prompt_file {prompt[0]} is another job's file; ws will not write over it", "rename it", 3
        )
    for f in [dest] + ([folder / prompt[0]] if prompt else []):
        if f.is_symlink():
            raise WsError("refused", f"{f} is a link; ws will not write through it", "remove the link", 3)
    replacing = dest.exists()
    old_text = ""
    base: Power | None = None
    if replacing:
        try:
            old_text = dest.read_text(encoding="utf-8")
            base = _power(tomllib.loads(old_text))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            base = Power()
        diff = _diff(old_text, raw.decode("utf-8"), dest.name)
        if prompt is not None:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                old_p = (folder / prompt[0]).read_text(encoding="utf-8") if (folder / prompt[0]).is_file() else ""
                diff += _diff(old_p, prompt[1].decode("utf-8", "replace"), prompt[0])
        if not args.replace:
            if diff and not args.json:
                print(diff, end="" if diff.endswith("\n") else "\n")
            raise WsError(
                "refused",
                f"job {name} is installed already" + ("" if diff else " and is the same"),
                f"`ws job add {args.path} --replace` puts the change above in",
            )
        pin = pins.get(name)
        if pin is not None and pin.get("sha256") == sha and jobs.file_digest(folder, name) == sha:
            print(f"ws: job {name} is installed and unchanged (0 changes)")
            return 0
        if diff and not args.json:
            print(diff, end="" if diff.endswith("\n") else "\n")
    widened = widening(_power(d), base, _installed(folder, name))
    approved = False
    if widened and _in_session(env):
        bound = approvals.Bound("ws", "job add", f"ws job add {name} sha256:{sha} adds {', '.join(widened)}", "", "")
        why = approvals.require(approvals.approvals_dir(), bound, why_asked=f"job {name} adds {', '.join(widened)}")
        if why is not None:
            raise WsError(
                "refused", str(why), "a person approves it once; then run the identical `ws job add` again", 3
            )
        approved = True

    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        jobs.check_private(folder)
        pins = jobs.read_pins(folder)
        if prompt is not None:
            _write_private(folder / prompt[0], prompt[1])
        _write_private(dest, raw)
        old = pins.get(name, {}).get("files", [])
        keep = {dest.name, *([prompt[0]] if prompt else [])} | _taken(folder, pins, name)
        for f in old if isinstance(old, list) else []:
            if isinstance(f, str) and Path(f).name == f and f not in keep:
                with contextlib.suppress(FileNotFoundError):
                    (folder / f).unlink()  # the prompt_file the replaced job had, which nothing uses now
        pins[name] = {
            "sha256": sha,
            "files": [dest.name] + ([prompt[0]] if prompt else []),
            "added": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _save_pins(folder, pins)
        _log(env, "replace" if replacing else "add", name, sha, widened, approved)
    except (OSError, jobs.JobError) as e:
        raise WsError("failed", f"job {name} could not be written to {folder}: {e}", "check the folder is yours") from e

    applied = ""
    code = 0
    if not args.no_apply and "schedule" in d:
        applied, code = _apply()
    if args.json:
        output.result(
            {
                "job": name,
                "sha256": sha,
                "replaced": replacing,
                "widened": widened,
                "approved": approved,
                "schedule": applied or None,
            }
        )
    else:
        print(
            f"ws: {'replaced' if replacing else 'added'} job {name} (sha256 {sha[:12]}, pinned)"
            + (f"; adds {', '.join(widened)}" if widened else "")
            + (f"; {applied}" if applied else "")
        )
    return code


def _apply() -> tuple[str, int]:
    """`ws schedule apply`, in-process. (what it said, its exit code); a failure is reported, the job stays added."""
    import io
    from contextlib import redirect_stdout

    from ws.roles import commands

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = commands.cmd_schedule_apply(argparse.Namespace(dry_run=False, json=False))
    except WsError as e:
        return f"timers not applied: {e.message} (run `ws schedule apply` once it is fixed)", 1
    return buf.getvalue().strip().removeprefix("ws: "), code


def cmd_remove(args: argparse.Namespace) -> int:
    env = os.environ
    name = args.name
    if not jobs.NAME.match(name):
        raise WsError("usage", "a job name is letters, digits, - and _", "", 2)
    folder = jobs.jobs_dir(env)
    try:
        pins = jobs.read_pins(folder) if folder.is_dir() else {}
    except jobs.JobError as e:
        raise WsError("bad_config", str(e), "") from e
    pin = pins.get(name)
    dest = folder / f"{name}.toml"
    if pin is None and not dest.exists():
        print(f"ws: no job {name} (0 changes)")
        return 0
    stopped = ""
    with contextlib.suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        if dest.is_file() and "schedule" in tomllib.loads(dest.read_text(encoding="utf-8")):
            from ws import osinfo
            from ws.roles import commands

            try:
                gone, _ = commands._drop(commands._scheduler(osinfo.detect()), env, [name])
                stopped = f"; removed {gone} unit file(s)"
            except (ValueError, OSError) as e:
                stopped = f"; its timer could not be stopped ({e}): run `ws schedule remove {name}`"
    files = pin.get("files", [dest.name]) if pin else [dest.name]
    shared = _taken(folder, pins, name)
    removed = []
    for f in files:
        if not isinstance(f, str) or Path(f).name != f or f in shared:
            continue
        with contextlib.suppress(FileNotFoundError):
            (folder / f).unlink()
            removed.append(f)
    if pin is not None:
        del pins[name]
        _save_pins(folder, pins)
    _log(env, "remove", name, pin.get("sha256") if pin else None, [], False)
    if args.json:
        output.result({"job": name, "removed": removed})
    else:
        print(f"ws: removed job {name} ({', '.join(removed) or 'no files'}){stopped}")
    return 0


def register(sub) -> None:
    j = sub.add_parser("job", help="add a job file (checked, copied and pinned) or remove one")
    js = j.add_subparsers(dest="job_command", required=True)
    p = js.add_parser("add", help="check a job file, copy it into the jobs folder and pin its sha256")
    p.add_argument("path", help="the job file, NAME.toml (its prompt_file is copied with it)")
    p.add_argument("--replace", action="store_true", help="replace an installed job of the same name (shows the diff)")
    p.add_argument("--no-apply", action="store_true", help="do not run `ws schedule apply` afterwards")
    p.set_defaults(func=cmd_add)
    p = js.add_parser("remove", help="stop a job's timer, delete what `ws job add` copied and drop its pin")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)
