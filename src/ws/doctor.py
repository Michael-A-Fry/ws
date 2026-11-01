"""`ws doctor`: one line per check, each with a stable ID. Exit 0 only when nothing is failing or unconfigured.

IDs never change meaning once released: WS0xx are the machine, profile, jobs and gate, WS1xx are the roles (one per role).
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ws import __version__, canary, jobs, roles, usage, userconfig
from ws import globals as ws_globals
from ws.osinfo import OSInfo
from ws.profile import ProfileError, load, profile_path

OK, WARN, FAIL, UNCONFIGURED = "ok", "warn", "fail", "unconfigured"
BAD = (FAIL, UNCONFIGURED)
SCHEMA = 1


@dataclass
class Check:
    id: str
    role: str
    status: str
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    what: str
    example: str  # the smallest [roles.<name>] table that configures it
    check: Callable[[dict, OSInfo, dict, bool], tuple[str, str]]


def _adapter(cfg: dict, known: dict[str, str]) -> tuple[str | None, tuple[str, str] | None]:
    a = cfg.get("adapter")
    if not isinstance(a, str) or not a:
        return None, (FAIL, "no adapter named")
    if a not in known:
        return None, (FAIL, f"unknown adapter {a!r}; known: {', '.join(sorted(known))}")
    return a, None


RUNNERS = {"claude-code": "claude", "opencode": "opencode"}


def _runner(cfg, osi, env, network):
    a, bad = _adapter(cfg, RUNNERS)
    if bad:
        return bad
    exe = shutil.which(RUNNERS[a], path=env.get("PATH"))
    if not exe:
        return FAIL, f"{a} selected but `{RUNNERS[a]}` is not on PATH"
    from ws import run  # reports the version only: ws never manages a runner's updates

    version = run.exe_version(exe, dict(env))
    if version is None:
        return FAIL, f"{a} found at {exe} but `{RUNNERS[a]} --version` did not run"
    detail = f"{a} {version} at {exe}" if version else f"{a} at {exe} (it printed no version)"
    return _version_change(a, version, detail, env, network)


def _default_model(env) -> str | None:
    try:
        roles = (load(profile_path(env)) or {}).get("roles")
    except ProfileError:
        return None
    cfg = roles.get("model") if isinstance(roles, dict) else None
    m = cfg.get("default") if isinstance(cfg, dict) else None
    return m if isinstance(m, str) and m else None


def _version_change(adapter: str, version: str, detail: str, env, network: bool) -> tuple[str, str]:
    """Remember the runner version and default model in versions.json. If either changed since the last look, say so and
    run the canary (or point to `ws canary` when offline). ws never forces an update; this just notices one."""
    model = _default_model(env)
    changes = canary.observe(env, adapter, version or None, model, record=False)
    if not changes:
        canary.observe(env, adapter, version or None, model)
        return OK, detail
    seen = f"changed since last seen ({'; '.join(changes)})"
    if not network:  # not remembered: this note stays until a canary passes (`ws canary` does that too)
        return OK, f"{detail}; {seen}: run `ws canary`"
    result = canary.run_canary([adapter], env, model)  # a pass remembers the new version
    if result["ok"]:
        return OK, f"{detail}; {seen}: canary passed"
    return FAIL, f"{detail}; {seen}: canary failed: {'; '.join(canary.failures(result))}"


def _model(cfg, osi, env, network):
    a = cfg.get("adapter")
    if isinstance(a, str) and a and a not in roles.adapters("model"):
        # a profile from before the model adapters: only the name was ever checked, and it still is
        status, detail = roles.get("model", "anthropic").check({"default": cfg.get("default")}, osi, env, network)
        return status, f"{detail}; adapter {a!r} has no check of its own"
    return roles.check("model", cfg, osi, env, network, default="anthropic")


def _budget(cfg, osi, env, network):
    cap = cfg.get("monthly_cap_usd")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return FAIL, "monthly_cap_usd must be a number above 0: ws refuses paid models without your own cap"
    return OK, f"monthly cap US${cap:g}"


def _secrets(cfg, osi, env, network):
    return roles.check("secrets", cfg, osi, env, network)


_folder = roles.folder


def _memory(cfg, osi, env, network):
    return roles.check("memory", cfg, osi, env, network, default="markdown-git")


def _notes(cfg, osi, env, network):
    d, bad = _folder(cfg)
    if bad:
        return bad
    inbox = d / "_inbox"
    if inbox.exists():
        if not inbox.is_dir():
            return FAIL, f"{inbox} exists but is not a folder"
        if not os.access(inbox, os.W_OK | os.X_OK):
            return FAIL, f"{inbox} is not writable"
    elif not os.access(d, os.W_OK | os.X_OK):
        return FAIL, f"{d} is not writable, so ws cannot make _inbox"
    return OK, f"{d}"


def _scheduler(cfg, osi, env, network):
    return roles.check("scheduler", cfg, osi, env, network)


def _notify(cfg, osi, env, network):
    return roles.check("notify", cfg, osi, env, network)


def _backup(cfg, osi, env, network):
    return roles.check("backup", cfg, osi, env, network)


def _usage(cfg, osi, env, network):
    a, bad = _adapter(cfg, {"anthropic": "", "none": ""})
    if bad:
        return bad
    if a == "none":
        return WARN, "no provider usage report: spend is known only from ws's own run log"
    name = cfg.get("key_env", "ANTHROPIC_ADMIN_KEY")
    key = env.get(name)
    if not key:
        return FAIL, f"${name} is not set (an Admin API key; organisations only, not individual accounts)"
    if not network:
        return WARN, "skipped (--offline)"
    return usage.check_anthropic(key)


ROLES = (
    Role("WS101", "runner", "the AI tool that does the work", 'adapter = "claude-code"', _runner),
    Role("WS102", "model", "the model each job uses by default", 'default = "<model id>"', _model),
    Role("WS103", "budget", "your own monthly spend cap", "monthly_cap_usd = 20", _budget),
    Role("WS104", "secrets", "where keys live; agents never see them", 'adapter = "ws-secret"', _secrets),
    Role("WS105", "memory", "the Markdown folder ws remembers in", 'path = "~/ws-memory"', _memory),
    Role("WS106", "notes", "your notes folder (any app)", 'path = "~/Notes"', _notes),
    Role("WS107", "scheduler", "runs jobs on a timer", 'adapter = "systemd"', _scheduler),
    Role("WS108", "notify", "how ws reaches you", 'adapter = "none"', _notify),
    Role("WS109", "backup", "proves your files can come back", 'adapter = "restic", repo = "<folder>"', _backup),
    Role("WS110", "usage", "the provider's own usage report", 'adapter = "anthropic"', _usage),
)


def _ptrace_check(osi) -> Check:
    """WS007: can a process in the wall read the memory of the runner that started it? With Yama's ptrace_scope at 0 (or
    no Yama) any process of the same user can, through `/proc/PID/mem` or ptrace, and a system-call filter cannot refuse
    the first. ptrace_scope 1 limits it to descendants of the target, so a child cannot reach its parent."""
    if osi.kind not in ("linux", "wsl"):
        return Check("WS007", "ptrace", OK, "not applicable on this system")
    from ws import sandbox

    scope = sandbox.ptrace_scope()
    if scope is not None and scope >= 1:
        return Check("WS007", "ptrace", OK, f"ptrace_scope is {scope}: a walled child cannot read its parent's memory")
    now = "absent (no Yama)" if scope is None else "0"
    fix = "echo 'kernel.yama.ptrace_scope = 1' | sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system"
    return Check(
        "WS007",
        "ptrace",
        WARN,
        f"ptrace_scope is {now}: a process in the wall can read the runner's memory through /proc/PID/mem (the OpenCode "
        "shim makes OpenCode undumpable, which covers it; other runners are not covered)",
        fix,
    )


def run_checks(osi: OSInfo, profile_file: Path, env: dict | None = None, network: bool = True) -> list[Check]:
    env = os.environ if env is None else env
    checks: list[Check] = []
    if osi.supported:
        checks.append(Check("WS001", "platform", OK, f"{osi.kind} ({osi.release}, {osi.arch})"))
    elif osi.kind == "windows":
        checks.append(
            Check("WS001", "platform", FAIL, "native Windows is not supported", "install WSL2 and run ws inside it")
        )
    else:
        checks.append(Check("WS001", "platform", FAIL, f"{osi.kind} is not supported", "use macOS, Linux or WSL2"))
    py = ".".join(map(str, sys.version_info[:3]))
    checks.append(Check("WS002", "python", OK if sys.version_info >= (3, 11) else FAIL, f"Python {py}"))
    try:
        prof = load(profile_file)
    except ProfileError as e:
        prof = None
        checks.append(Check("WS003", "profile", FAIL, f"does not parse: {e}", f"fix or remove {profile_file}"))
    else:
        if prof is None:
            checks.append(Check("WS003", "profile", UNCONFIGURED, f"no profile at {profile_file}", "run `ws init`"))
        else:
            checks.append(Check("WS003", "profile", OK, str(profile_file)))
    prof = prof or {}
    machine = prof.get("machine") if isinstance(prof.get("machine"), dict) else {}
    missing = [k for k in ("stays_on", "server", "notes") if k not in machine]
    if missing:
        checks.append(Check("WS004", "machine", UNCONFIGURED, f"not answered: {', '.join(missing)}", "run `ws init`"))
    else:
        on = "stays on" if machine["stays_on"] else "not always on"
        checks.append(Check("WS004", "machine", OK, f"{on}; server: {machine['server'] or 'none'}"))
    try:
        status, detail, fix = ws_globals.doctor_check(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        status, detail, fix = FAIL, f"check crashed ({type(e).__name__})", ""
    checks.append(Check("WS005", "globals", status, detail, fix))
    try:
        bad_jobs = jobs.problems(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS006", "jobs", FAIL, f"check crashed ({type(e).__name__})"))
    else:
        if bad_jobs:
            first = next(iter(bad_jobs.values()))
            detail = f"{len(bad_jobs)} job file(s) ws would refuse ({', '.join(bad_jobs)}): {first}"
            fix = "fix or remove that job file; the other jobs still run"
            checks.append(Check("WS006", "jobs", FAIL, detail, fix))
        else:
            checks.append(Check("WS006", "jobs", OK, "every job file loads"))
    checks.append(_ptrace_check(osi))
    try:
        status, detail, fix = userconfig.check(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        status, detail, fix = FAIL, f"check crashed ({type(e).__name__})", ""
    checks.append(Check("WS008", "gate", status, detail, fix))
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    for r in ROLES:
        cfg = roles.get(r.name)
        fix = f"add [roles.{r.name}] with {r.example} to the profile"
        if not isinstance(cfg, dict):
            checks.append(Check(r.id, r.name, UNCONFIGURED, f"not set up: {r.what}", fix))
            continue
        try:
            status, detail = r.check(cfg, osi, env, network)
        except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
            status, detail = FAIL, f"check crashed ({type(e).__name__}); details withheld in case they hold a secret"
        checks.append(Check(r.id, r.name, status, detail, fix if status in BAD else ""))
    return checks


def report(checks: list[Check], osi: OSInfo, profile_file: Path) -> dict:
    return {
        "schema": SCHEMA,
        "version": __version__,
        "os": osi.as_dict(),
        "profile": str(profile_file),
        "ok": not any(c.status in BAD for c in checks),
        "checks": [asdict(c) for c in checks],
    }


def render(checks: list[Check], osi: OSInfo) -> str:
    out = [f"ws doctor {__version__} on {osi.kind} ({osi.release}, {osi.arch})", ""]
    w = max(len(c.role) for c in checks)
    for c in checks:
        out.append(f"{c.id}  {c.status:<12}  {c.role:<{w}}  {c.detail}")
        if c.fix:
            out.append(f"{'':5}  {'':12}  {'':<{w}}  fix: {c.fix}")
    counts = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL, UNCONFIGURED)}
    out += ["", f"{len(checks)} checks: " + ", ".join(f"{n} {s}" for s, n in counts.items())]
    return "\n".join(out)
