"""The acceptance checks `ws upgrade --unattended` runs after a swap, the snapshot they are compared with, and the release log.

Nothing starts a session: the gate is probed the way a watchdog would, by feeding the PreToolUse hook that is written in
the runner's settings (Claude Code's `settings.json`, found as the runner finds it, `CLAUDE_CONFIG_DIR` included) and the
check command in OpenCode's plugin a Bash payload and reading the exit code (0 lets the call run, Claude Code's 2 and
any non-zero from OpenCode's check stop it). That is the check a fresh session would meet first, without spending a
subscription run on it. `--session-check COMMAND` adds one of the person's own on top. A probe sets `WS_GATE_DAEMON=0`, so
a hook decides in its own process: a daemon started by the probe would live in the service's own cgroup and be killed
with it (the daemon path is tested with the gate itself, not here).

`snapshot` is taken before the swap and `acceptance` compares with it, never with a constant: the number of deny rules in
the settings, and how many of the must-deny probes below the hook stops. A release may add rules and probes' answers; one
that has fewer of either than before is a gate that got weaker, and the swap is rolled back. The probes are a short fixed
list in this file, the same on every machine and in every release: the larger gate corpus lives in the source tree and
is not shipped, so it can't be replayed on an installed ws.

The release log (`<state>/release-log.csv`: at, verb, from, to, duration_s, result, reason) has one row for each promote,
auto-rollback, rollback and rollback drill the service did, and each request it dropped or could not act on. It is a
plain CSV any tool can read.
"""

from __future__ import annotations

import csv
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from ws import gateclient, jsonl, paths, userconfig

LOG_COLUMNS = ("at", "verb", "from", "to", "duration_s", "result", "reason")
PROBE_TIMEOUT_S = (
    gateclient.WORST_CASE_S + 6.0
)  # a hook may take its worst case (the wait for the daemon, then the in-process check)
STEP_ENV = "WS_UPGRADE_STEP"  # the same name as ws.upgrade.STEP_ENV (a test holds the two together)
ALLOW_PROBE = "ls -la"


def deny_probes(env: Mapping[str, str]) -> tuple[str, ...]:
    """Commands the gate must stop, for this person's home and secrets store (so no path here is a real one): a private
    key read, a read in the secrets folder, a forced push to main and a delete of the root."""
    home, secrets = env.get("HOME", ""), paths.data_dir(dict(env)) / "secrets"
    return (f"cat {home}/.ssh/id_rsa", f"cat {secrets}/probe", "git push --force origin main", "rm -rf /")


def log_path(env: Mapping[str, str]) -> Path:
    return paths.state_dir(dict(env)) / "release-log.csv"


def log_row(
    env: Mapping[str, str], verb: str, frm: str, to: str | None, seconds: float, result: str, reason: str = ""
) -> None:
    """Add one row to the release log (the header first, in a file that is new). A log that can't be written is said once
    on stderr, which the journal keeps, and never stops the upgrade."""
    path = log_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.is_file()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_COLUMNS)
            w.writerow([jsonl.iso_utc(time.time()), verb, frm, to or "", round(seconds), result, reason[:300]])
    except OSError as e:
        print(f"ws: could not write {path} ({type(e).__name__})", file=sys.stderr)


def last_rows(env: Mapping[str, str]) -> list[dict[str, str]]:
    """The log's rows. A row that is cut short (a write that died half way) or carries a stray column is dropped, so a
    caller that reads a column never meets a None."""
    try:
        with open(log_path(env), newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return [r for r in rows if None not in r and all(isinstance(r.get(c), str) for c in LOG_COLUMNS)]


def settings_path(env: Mapping[str, str]) -> Path:
    """Claude Code's user settings, where the runner reads them (`CLAUDE_CONFIG_DIR` first, as `ws install` writes them)."""
    return userconfig.target("claude-code", env)


def _settings(env: Mapping[str, str]) -> dict:
    try:
        data = json.loads(settings_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_command(env: Mapping[str, str]) -> str | None:
    """The PreToolUse hook command that is ws's gate, as the settings name it, or None."""
    for group in _settings(env).get("hooks", {}).get("PreToolUse", []):
        for h in group.get("hooks", []) if isinstance(group, dict) else []:
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if "gate check" in cmd or "gateclient" in cmd:
                return cmd
    return None


def opencode_check(env: Mapping[str, str]) -> list[str] | None:
    """The argv OpenCode's plugin runs before each tool call (the `const CHECK = [...]` line `ws install` wrote), or None
    when there is no such plugin."""
    try:
        text = userconfig.plugin_path(env).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = re.search(r"^const CHECK = (\[.*\])\s*$", text, re.MULTILINE)
    try:
        argv = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    return argv if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) else None


class Gate(NamedTuple):
    """One runner's gate entry as the runner would run it: its name, the argv, and the code that means "stopped"."""

    runner: str
    argv: list[str]


def gates(env: Mapping[str, str]) -> list[Gate]:
    """The gates a session would meet first: Claude Code's PreToolUse hook and OpenCode's plugin check, each only when ws
    wrote it."""
    found = []
    hook = hook_command(env)
    if hook is not None:
        found.append(Gate("claude-code", ["sh", "-c", hook]))
    check = opencode_check(env)
    if check is not None:
        found.append(Gate("opencode", check))
    return found


def stopped(gate: Gate, code: int) -> bool:
    """Claude Code's hook says stop with 2 (and nothing else is a stop); OpenCode's plugin blocks on any non-zero."""
    return code == 2 if gate.runner == "claude-code" else code != 0


def probe(hook: str | Gate, command: str, env: Mapping[str, str]) -> int:
    """The gate's exit code for one Bash call, run as the runner runs it (a hook through `sh -c`, the payload on stdin).
    A gate that does not answer in time is 124. `hook` is Claude Code's hook command, or a `Gate`."""
    gate = hook if isinstance(hook, Gate) else Gate("claude-code", ["sh", "-c", hook])
    if gate.runner == "claude-code":
        payload = {
            "session_id": "ws-upgrade-check",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": env.get("HOME", ""),
        }
    else:
        payload = {
            "hook_event_name": "tool.execute.before",
            "tool": "bash",
            "args": {"command": command},
            "session_id": "ws-upgrade-check",
            "cwd": env.get("HOME", ""),
        }
    try:
        done = subprocess.run(
            gate.argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
            env={**env, "WS_GATE_DAEMON": "0"},
        )
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
    return done.returncode


def snapshot(env: Mapping[str, str]) -> dict[str, int]:
    """What the gate stops now: `deny` rules in the settings, `denied` of the must-deny probes the gates stop (summed over
    each runner's gate), and `allow` rules (recorded for the log, not compared)."""
    perms = _settings(env).get("permissions", {})
    perms = perms if isinstance(perms, dict) else {}
    denied = sum(stopped(g, probe(g, c, env)) for g in gates(env) for c in deny_probes(env))
    return {
        "deny": len(perms.get("deny", [])) if isinstance(perms.get("deny"), list) else 0,
        "denied": denied,
        "allow": len(perms.get("allow", [])) if isinstance(perms.get("allow"), list) else 0,
    }


def acceptance(
    env: Mapping[str, str],
    tag: str,
    ws_argv: list[str],
    before: Mapping[str, int] | None,
    session_check: str | None = None,
) -> list[str]:
    """The problems found after a swap to `tag` (none means every check passed). `ws_argv` starts the new ws. `before` is
    the snapshot taken before the swap: a snapshot with a count missing is a problem (nothing to compare with), never a
    quiet pass. `None` says there is none to compare with (a rollback) and is printed on stderr."""
    problems: list[str] = []

    def ws(*args: str) -> subprocess.CompletedProcess | None:
        try:
            # the step marker is what lets `ws upgrade --hooks-only` run with no terminal under the service (ws.upgrade.STEP_ENV)
            return subprocess.run(
                [*ws_argv, *args], capture_output=True, text=True, timeout=60, check=False, env={**env, STEP_ENV: "1"}
            )
        except (OSError, subprocess.SubprocessError):
            return None

    ran = ws("--version")
    words = ran.stdout.split() if ran else []
    if not ran or ran.returncode != 0 or words[-1:] != [tag.removeprefix("v")]:
        problems.append(f"`ws --version` does not say {tag.removeprefix('v')} (it says {' '.join(words) or 'nothing'})")
    hooks = ws("upgrade", "--hooks-only")
    if hooks is None or hooks.returncode != 0:
        problems.append(f"`ws upgrade --hooks-only` exits {hooks.returncode if hooks else 'without running'}")
    found = gates(env)
    home = env.get("HOME", "")
    if not found:
        problems.append("no gate hook in the runner's settings after the swap")
    for gate in found:
        if probe(gate, ALLOW_PROBE, env) != 0:
            problems.append(f"the {gate.runner} gate stopped `{ALLOW_PROBE}`")
        for cmd in deny_probes(env)[:2]:
            if not stopped(gate, probe(gate, cmd, env)):
                problems.append(f"the {gate.runner} gate let `{cmd.replace(home, '~')}` through")
    if before is None:  # a rollback may land on a release with fewer rules: said, never silent
        print("ws: the gate is not compared with a snapshot (none was asked for)", file=sys.stderr)
        now = {}
    else:
        now = snapshot(env)
    for key, what in (("deny", "deny rules"), ("denied", "must-deny probes stopped")):
        if before is None:
            break
        if not isinstance(before.get(key), int):
            problems.append(f"the snapshot taken before the swap has no `{key}` count, so the gate was not compared")
        elif now[key] < before[key]:
            problems.append(f"{what}: {now[key]}, fewer than the {before[key]} before the swap")
    if session_check:
        try:
            ok = subprocess.run(shlex.split(session_check), timeout=PROBE_TIMEOUT_S * 6, check=False, env=dict(env))
        except (OSError, ValueError, subprocess.SubprocessError):
            problems.append("the session check could not run")
        else:
            if ok.returncode != 0:
                problems.append(f"the session check exits {ok.returncode}")
    return problems
