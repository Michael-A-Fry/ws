"""`ws doctor`: one line per check, each with a stable ID. Exit 0 only when nothing is failing or unconfigured.

IDs never change meaning once released: WS0xx are the machine, profile, jobs, gate,
sandbox, plugins, where ws keeps its files, what the runner needs of its hooks, the OS-user classes, the network of each data class, the settings keys the wall writes and the guards that ask to log, WS1xx are the roles
(one per role).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import __version__, canary, jobs, paths, policy, roles, usage, userconfig, wallsettings
from ws import globals as ws_globals
from ws.osinfo import OSInfo
from ws.profile import ProfileError, load, profile_path
from ws.style import Style, plural

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


def _plugins_check(prof: dict, env) -> list[Check]:
    """WS009: a plugin the profile switches on that ws does not offer yet. One note, nothing else changes; no line when
    there is none."""
    from ws import modules  # late: modules pulls in the install engine

    table = prof.get("modules")
    on = {n for n, v in table.items() if v is True} if isinstance(table, dict) else set()
    names = modules.unbuilt_on(on, env)
    if not names:
        return []
    verb = "is" if len(names) == 1 else "are"
    detail = f"{', '.join(names)} {verb} on in your profile and still being built; no effect yet"
    return [Check("WS009", "plugins", WARN, detail, f"to hide it, run `ws plugin remove {names[0]}`")]


MIN_CLAUDE_HOOKS = (2, 1, 295)  # the first Claude Code that lets a hook say a failure blocks (`onFailure`)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def _claude_version(env) -> tuple[bool, str | None]:
    """(is `claude` on PATH, what `claude --version` prints). The one probe WS011 and WS016 share; the version is None
    when it did not run or printed nothing a version can be read from."""
    from ws import run  # late, like WS101: it reports the version only

    exe = shutil.which(RUNNERS["claude-code"], path=env.get("PATH"))
    if not exe:
        return False, None
    return True, run.exe_version(exe, dict(env))


def _hooks_check(prof: dict, env) -> list[Check]:
    """WS011: Claude Code is new enough for the gate's hooks to block when they fail (`onFailure`, from 2.1.295). Before
    that a hook that crashes or times out lets the call through. No line unless the profile's runner is Claude Code and
    `claude` is on PATH (WS101 reports a missing runner)."""
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    runner = roles.get("runner")
    if not isinstance(runner, dict) or runner.get("adapter") != "claude-code":
        return []
    installed, version = _claude_version(env)
    if not installed:
        return []
    need = ".".join(map(str, MIN_CLAUDE_HOOKS))
    found = _version_tuple(version) if version else None
    if found is None:
        detail = f"can't tell the Claude Code version, and the gate's hooks need {need} or newer to block on a failure"
        return [Check("WS011", "hooks", WARN, detail, "update Claude Code, then run `ws doctor` again")]
    if found < MIN_CLAUDE_HOOKS:
        detail = f"Claude Code {version} lets a hook that fails or times out through; the gate needs {need} or newer"
        return [Check("WS011", "hooks", FAIL, detail, "update Claude Code")]
    return [Check("WS011", "hooks", OK, f"Claude Code {version}: a hook that fails blocks the call")]


def _upgrade_check(env) -> Check:
    """WS013: the hooks in the runner's config are as new as the ws that runs them. A hook written before ws pinned its
    folders still works (it runs the full check and says so on every call), so this is a `warn`, not a `fail`. A runner
    whose config can't be read is named too (a link, JSONC): what its hooks carry is not known, and `ws upgrade` leaves
    it alone."""
    state = userconfig.hook_state(env)
    if not state.old and not state.unreadable:
        return Check("WS013", "upgrade", OK, "ws's hooks are as new as this ws")
    parts = []
    if state.old:
        parts.append(
            f"the hooks in {', '.join(state.old)} are older than this ws and carry no pin; each call prints a note"
        )
    parts += [f"the hooks in {runner} can't be checked: {why}" for runner, why in state.unreadable.items()]
    fix = "run `ws upgrade --hooks-only` in a terminal of your own (a gated session can't)"
    if state.unreadable:
        fix = "make the config a plain JSON file ws can read, then " + fix
    return Check("WS013", "upgrade", WARN, "; ".join(parts), fix)


def _class_checks(env, kind: str) -> list[Check]:
    """WS014: each class with an `os_user` in the policy, one line per part, each judged on its own (the OS user exists, the
    class file is there and root-owned, the sandbox is required, unsandboxed commands are off, bypass is off,
    writes are limited, the home folder is hidden, inbound is lead-only). No line when the policy names no such class, or
    does not load (WS012 says so). The user and the file are read from the system, never from the environment."""
    try:
        pol = policy.load(policy.policy_path(dict(env)))
    except (policy.PolicyError, OSError):
        return []
    rows = wallsettings.class_rows(pol, env, kind)
    return [Check("WS014", "class", r.status, f"{r.cls}: {r.part}: {r.detail}", r.fix) for r in rows]


def _review_checks(env, now=None) -> list[Check]:
    """WS017: each class that sets a `review_date`, one line: `warn` when the date is today or past (`Class.review_due`, UTC:
    the one judge `ws walls check` also asks), `ok` with the owner and date otherwise. No line for a class with no date, when
    the policy has no classes, or when it does not load (WS012 says so)."""
    try:
        pol = policy.load(policy.policy_path(dict(env)))
    except (policy.PolicyError, OSError):
        return []
    now = datetime.now(UTC) if now is None else now
    out = []
    for name in sorted(pol.classes):
        cls = pol.classes[name]
        if cls.review_date is None:
            continue
        who = f"owner {cls.owner}" if cls.owner else "no owner named"
        if cls.review_due(now):
            fix = (
                f"look at the class, then move review_date under [classes.{name}] to the next review (`ws walls edit`)"
            )
            out.append(Check("WS017", "review", WARN, f"{name}: review was due {cls.review_date} ({who})", fix))
        else:
            out.append(Check("WS017", "review", OK, f"{name}: next review {cls.review_date} ({who})"))
    return out


_MODE_FIX = {
    "native-windows": 'run ws inside WSL2 (the wall needs Linux or macOS), or take mode = "log" out of those guards',
    "no-sandbox-binary": (
        'install bubblewrap (Linux, WSL2; macOS has sandbox-exec), or take mode = "log" out of those guards'
    ),
    "sandbox-off": 'set sandbox = true under [gate] and run `ws install`, or take mode = "log" out of those guards',
}


def _mode_checks(env) -> list[Check]:
    """WS018: the guards that set `mode = "log"`, one line. `warn` when this machine cannot give the wall (native Windows, no
    sandbox binary on PATH, or `[gate] sandbox = false`): each of those guards then refuses as in enforce, and the line
    names them and the one word for why. `ok` when it can (whether the wall holds for a given call is the gate's to say,
    call by call). No line when no guard asks for log, or when the policy does not load (WS012 says so). It names guards
    and a reason, never a path."""
    from ws import gate, guards  # here, not at the top: the gate imports a lot the doctor does not need

    try:
        pol = policy.load(policy.policy_path(dict(env)))
    except (policy.PolicyError, OSError):
        return []
    asked = guards.requested_log(pol)
    if not asked:
        return []
    names = ", ".join(asked)
    gap = gate.machine_gap() or ("" if pol.gate.sandbox else "sandbox-off")
    if gap:
        detail = f"{names} ask to log what the wall enforces, and the wall cannot apply here ({gap}): they refuse as in enforce"
        return [Check("WS018", "guard-modes", WARN, detail, _MODE_FIX[gap])]
    return [Check("WS018", "guard-modes", OK, f"{names} log what the wall enforces, call by call where it is proven")]


def _egress_checks(env, kind: str, now=None) -> list[Check]:
    """WS015: the network of each data class, one line per class, from the policy (nothing is probed). `ok` when every
    host entry can be read by the proxies and no egress exception has lapsed or ends within a week; `warn` otherwise,
    with the count, never a host name. A class file in place whose network block is not what ws renders now is a `warn`
    as well. No line when the policy has no classes, or does not load (WS012 says so)."""
    from ws import sandbox  # late: ws.sandbox imports the adapters

    try:
        pol = policy.load(policy.policy_path(dict(env)))
    except (policy.PolicyError, OSError):
        return []
    now = datetime.now(UTC) if now is None else now
    stale = set(wallsettings.stale_networks(pol, env, kind))
    out = []
    for name in sorted(pol.classes):
        cls = pol.classes[name]
        plan = sandbox.egress_plan(pol, cls, "", now)
        mine = [x for x in pol.egress_exceptions if name in x.classes]
        soon = [x.name for x in mine if not x.expired(now) and x.expires - now < timedelta(days=7)]
        lapsed = [n for n, _ in plan.lapsed]
        issues = []
        if plan.dropped:
            issues.append(f"{plural(len(plan.dropped), 'host entry', 'host entries')} the proxies can't read")
        if lapsed:
            issues.append(f"expired exception {', '.join(map(repr, lapsed))} covers nothing")
        if soon:
            issues.append(f"exception {', '.join(map(repr, soon))} ends within 7 days")
        if not pol.gate.sandbox:
            issues.append(
                "[gate] sandbox is off, so no network list is applied to Claude Code and its launches are refused"
            )
        if name in stale:
            issues.append("the class file's network list is not what the policy renders now")
        if issues:
            fix = (
                "fix allowed_hosts (a name with a dot, or localhost:PORT), renew or remove the exception, or run "
                f"`ws install --class {name}`"
            )
            out.append(Check("WS015", "egress", WARN, f"{name}: {'; '.join(issues)}", fix))
        else:
            n = len(sandbox.class_domains(cls))
            held = f", {len(plan.granted)} exception(s) in force" if plan.granted else ""
            out.append(Check("WS015", "egress", OK, f"{name}: network shut but for {n} host(s){held}"))
    return out


def _wall_keys_check(env) -> list[Check]:
    """WS016: Claude Code is new enough to read the settings keys the wall writes (wallsettings.MIN_CLAUDE, the one table of
    key -> first version; the version is read as WS011 reads it). `ok` when it is, `warn` (update Claude Code) when it is
    older, or when its version can't be read, and `ok` "not applicable" with no Claude Code on PATH. A second `warn` line
    when `permissions.defaultMode` is `bypassPermissions` in the settings files in force (ws never writes it)."""
    installed, version = _claude_version(env)
    if not installed:
        return [Check("WS016", "wall-keys", OK, "not applicable: Claude Code is not installed")]
    try:
        pol = policy.load(policy.policy_path(dict(env)))
    except (policy.PolicyError, OSError):
        pol = policy.default_policy()  # WS012 says the policy doesn't load; judge the keys the default policy writes
    keys = wallsettings.keys_written(pol)
    found = _version_tuple(version) if version else None
    if not keys:
        out = [Check("WS016", "wall-keys", OK, "the wall writes no settings key that needs a newer Claude Code")]
        return out + _bypass_lines(env)
    need = ", ".join(f"{k} ({'.'.join(map(str, wallsettings.MIN_CLAUDE[k]))})" for k in keys)
    if found is None:
        detail = f"can't tell the Claude Code version, and the wall writes keys that need a recent one: {need}"
        out = [Check("WS016", "wall-keys", WARN, detail, "update Claude Code, then run `ws doctor` again")]
    else:
        old = [k for k in keys if found < wallsettings.MIN_CLAUDE[k]]
        if old:
            what = ", ".join(f"{k} (needs {'.'.join(map(str, wallsettings.MIN_CLAUDE[k]))})" for k in old)
            detail = f"Claude Code {version} does not read {what}: that part of the wall is not in force"
            out = [Check("WS016", "wall-keys", WARN, detail, "update Claude Code")]
        else:
            n = plural(len(keys), "wall key")
            out = [Check("WS016", "wall-keys", OK, f"Claude Code {version} reads the {n} ws writes")]
    return out + _bypass_lines(env)


def _bypass_lines(env) -> list[Check]:
    """The WS016 warning for `permissions.defaultMode: bypassPermissions` in the settings files in force (none otherwise)."""
    from ws import status  # late: it pulls in the adapters and the globals scan

    cwd = Path.cwd()
    mode = status.permission_mode(ws_globals.find_repo(cwd) or cwd, dict(env))
    if mode["mode"] != status.BYPASS_MODE:
        return []
    where = mode["source"]
    detail = (
        f"permissions.defaultMode is bypassPermissions in {where}: Claude Code runs a tool without asking "
        "(the gate's deny rules and the sandbox still apply)"
    )
    return [
        Check("WS016", "wall-keys", WARN, detail, f"take defaultMode out of {where}, or set it to a mode that asks")
    ]


def _layout_check(env) -> Check:
    """WS010: where ws keeps its files. `ok` for the XDG folders or a folder you named; `warn` while they are still in
    `~/.ws` from an earlier ws (nothing moves by itself: `ws migrate` does it); `fail` when both exist, because ws can't
    tell which holds the truth."""
    pr = paths.probe(env)
    if pr.state == "both":
        found = ", ".join(str(p) for p in pr.owned[:3]) + (" ..." if len(pr.owned) > 3 else "")
        detail = f"ws files are in two places: {pr.legacy} and {found}"
        return Check(
            "WS010", "layout", FAIL, detail, "run `ws migrate` to finish the move, or move the old folder aside"
        )
    if pr.state == "legacy":
        detail = f"ws files are still in {pr.legacy}, where an earlier ws kept them"
        return Check("WS010", "layout", WARN, detail, "run `ws migrate --dry-run` to see the move, then `ws migrate`")
    lay = paths.layout(env)
    if lay.mode == "single" and len(lay.distinct()) == 1:
        return Check("WS010", "layout", OK, f"one folder: {lay.config}")
    return Check("WS010", "layout", OK, f"config {lay.config}; state {lay.state}; data {lay.data}")


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
            detail = f"{plural(len(bad_jobs), 'job file')} ws would refuse ({', '.join(bad_jobs)}): {first}"
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
    try:
        over = userconfig.overridden(env)
    except Exception:  # noqa: BLE001 (the main WS008 line already says whether the gate is in place)
        over = []
    fix = "remove that allow rule, or take the deny rule out of the policy and run `ws install`"
    checks += [Check("WS008", "gate", WARN, line, fix) for line in over]
    try:
        status, detail, fix = wallsettings.doctor_check(env, osi.kind)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        status, detail, fix = FAIL, f"check crashed ({type(e).__name__})", ""
    checks.append(Check("WS012", "sandbox", status, detail, fix))
    try:
        checks.append(_upgrade_check(env))
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS013", "upgrade", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _class_checks(env, osi.kind)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS014", "class", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _egress_checks(env, osi.kind)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS015", "egress", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _wall_keys_check(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS016", "wall-keys", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _review_checks(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS017", "review", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _mode_checks(env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS018", "guard-modes", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks.append(_layout_check(env))
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS010", "layout", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _hooks_check(prof, env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS011", "hooks", FAIL, f"check crashed ({type(e).__name__})"))
    try:
        checks += _plugins_check(prof, env)
    except Exception as e:  # noqa: BLE001 (a broken check must report, not crash the whole doctor)
        checks.append(Check("WS009", "plugins", FAIL, f"check crashed ({type(e).__name__})"))
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


# The glyph a status wears. Unconfigured is an amber `!` like a warning, but it still counts as needing doing.
KIND = {OK: "ok", WARN: "warn", FAIL: "fail", UNCONFIGURED: "warn"}
_STATUS_WIDTH = 2 + max(len(s) for s in KIND)  # the glyph, a space, then the longest word


def render(checks: list[Check], osi: OSInfo, st: Style | None = None) -> str:
    st = st or Style()
    out = [st.heading(f"ws doctor {__version__} on {osi.kind} ({osi.release}, {osi.arch})"), ""]
    w = max(len(c.role) for c in checks)
    for c in checks:
        detail = st.muted(c.detail) if c.status == OK else c.detail  # steady states stay quiet
        out.append(f"{c.id}  {st.status(KIND[c.status], c.status, _STATUS_WIDTH)}  {c.role:<{w}}  {detail}")
        if c.fix:
            out.append(f"{'':5}  {'':{_STATUS_WIDTH}}  {'':<{w}}  {st.muted('fix:')} {c.fix}")
    counts = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL, UNCONFIGURED)}
    out += ["", f"{plural(len(checks), 'check')}: " + ", ".join(f"{n} {s}" for s, n in counts.items())]
    if counts[UNCONFIGURED]:
        n = counts[UNCONFIGURED]
        out.append(st.warn(f"{n} unconfigured: run `ws setup` to set {'it' if n == 1 else 'them'} up."))
    return "\n".join(out)
