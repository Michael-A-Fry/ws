"""`ws doctor`: one line per check, each with a stable ID. Exit 0 only when nothing is failing or unconfigured.

IDs never change meaning once released: WS0xx are the machine and profile, WS1xx are the roles (one per role).
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ws import __version__, broker, usage
from ws import secrets as wsecrets
from ws.osinfo import OSInfo
from ws.profile import ProfileError, load

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
    return (OK, f"{a} found at {exe}") if exe else (FAIL, f"{a} selected but `{RUNNERS[a]}` is not on PATH")


def _model(cfg, osi, env, network):
    m = cfg.get("default")
    if not isinstance(m, str) or not m:
        return FAIL, "no default model named"
    return OK, f"default model {m} (named; not probed yet)"


def _budget(cfg, osi, env, network):
    cap = cfg.get("monthly_cap_usd")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return FAIL, "monthly_cap_usd must be a number above 0: ws refuses paid models without your own cap"
    return OK, f"monthly cap US${cap:g}"


def _secrets(cfg, osi, env, network):
    a, bad = _adapter(cfg, {"ws-secret": "", "sops-age": "", "env": ""})
    if bad or a != "ws-secret":
        return bad or (OK, f"{a}")
    store, sock = wsecrets.Store(wsecrets.store_dir(env, cfg)), wsecrets.socket_path(env, cfg)
    try:
        problems, owner = wsecrets.health(wsecrets.registry_path(env), store)
    except PermissionError:  # the store belongs to another user: only the broker can vouch for it
        problems, owner = None, None
    try:
        answer, down = broker.status(sock), ""
    except broker.BrokerError as e:
        answer, down = None, str(e)
    if problems is None:
        if answer is None:
            return FAIL, f"the store belongs to another user and the broker is not answering: {down}"
        problems = answer.get("problems", ["the broker did not report on its store"])
        owner = answer.get("store_owner_uid")
    if problems:
        return FAIL, "; ".join(problems)
    if owner == os.geteuid():
        tail = "" if answer else f"; broker not answering ({down})"
        return WARN, f"same-user boundary: run `ws secret wall` for the one sudo step{tail}"
    if answer is None:
        return FAIL, f"store is walled but the broker is not answering: {down}"
    return OK, f"ws-secret: store owned by {wsecrets.owner_name(owner)}, broker answering"


def _dir_role(cfg, osi, env, network):
    p = cfg.get("path")
    if not isinstance(p, str) or not p:
        return FAIL, "no path set"
    d = Path(p).expanduser()
    return (OK, f"{d}") if d.is_dir() else (FAIL, f"{d} is not a folder")


def _scheduler(cfg, osi, env, network):
    a, bad = _adapter(cfg, {"systemd": "", "launchd": ""})
    if bad:
        return bad
    if a != osi.scheduler:
        return FAIL, f"{a} selected but this machine runs {osi.scheduler}"
    return OK, a


def _notify(cfg, osi, env, network):
    a, bad = _adapter(cfg, {"ntfy": "", "none": ""})
    return bad or (OK, "off (no pushes)" if a == "none" else a)


def _backup(cfg, osi, env, network):
    a, bad = _adapter(cfg, {"restic": ""})
    return bad or (OK, a)


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
    Role("WS105", "memory", "the Markdown folder ws remembers in", 'path = "~/ws-memory"', _dir_role),
    Role("WS106", "notes", "your notes folder (any app)", 'path = "~/Notes"', _dir_role),
    Role("WS107", "scheduler", "runs jobs on a timer", 'adapter = "systemd"', _scheduler),
    Role("WS108", "notify", "how ws reaches you", 'adapter = "none"', _notify),
    Role("WS109", "backup", "proves your files can come back", 'adapter = "restic"', _backup),
    Role("WS110", "usage", "the provider's own usage report", 'adapter = "anthropic"', _usage),
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
