"""The acceptance checks `ws upgrade --unattended` runs after a swap, the snapshot they are compared with, and the release log.

Nothing starts a session: the gate is probed the way a watchdog would, by feeding the PreToolUse hook that is written in
the runner's settings a Bash payload and reading its exit code (0 lets the call run, 2 stops it). That is the check a
fresh session would meet first, without spending a subscription run on it. `--session-check COMMAND` adds one of the
person's own on top.

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
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from ws import jsonl, paths

LOG_COLUMNS = ("at", "verb", "from", "to", "duration_s", "result", "reason")
PROBE_TIMEOUT_S = 20.0
STEP_ENV = "WS_UPGRADE_STEP"  # the same name as ws.upgrade.STEP_ENV (a test holds the two together)
ALLOW_PROBE = "ls -la"
# commands the gate must stop; HOME is the person's home, so no path here is a real one
DENY_PROBES = (
    "cat {HOME}/.ssh/id_rsa",
    "cat {HOME}/.ws/secrets/probe",
    "git push --force origin main",
    "rm -rf /",
)


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
    try:
        with open(log_path(env), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def settings_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".claude" / "settings.json"


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


def probe(hook: str, command: str, env: Mapping[str, str]) -> int:
    """The hook's exit code for one Bash call, run as the runner runs it (`sh -c`, the payload on stdin). A hook that does
    not answer in time is 124."""
    payload = {
        "session_id": "ws-upgrade-check",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": env.get("HOME", ""),
    }
    try:
        done = subprocess.run(
            ["sh", "-c", hook],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
    return done.returncode


def snapshot(env: Mapping[str, str]) -> dict[str, int]:
    """What the gate stops now: `deny` rules in the settings, `denied` of the must-deny probes the hook stops, and
    `allow` rules (recorded for the log, not compared)."""
    perms = _settings(env).get("permissions", {})
    perms = perms if isinstance(perms, dict) else {}
    hook = hook_command(env)
    home = env.get("HOME", "")
    denied = 0 if hook is None else sum(probe(hook, c.format(HOME=home), env) == 2 for c in DENY_PROBES)
    return {
        "deny": len(perms.get("deny", [])) if isinstance(perms.get("deny"), list) else 0,
        "denied": denied,
        "allow": len(perms.get("allow", [])) if isinstance(perms.get("allow"), list) else 0,
    }


def acceptance(
    env: Mapping[str, str], tag: str, ws_argv: list[str], before: Mapping[str, int], session_check: str | None = None
) -> list[str]:
    """The problems found after a swap to `tag` (none means every check passed). `ws_argv` starts the new ws."""
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
    hook = hook_command(env)
    home = env.get("HOME", "")
    if hook is None:
        problems.append("no gate hook in the runner's settings after the swap")
    else:
        if probe(hook, ALLOW_PROBE, env) != 0:
            problems.append(f"the gate stopped `{ALLOW_PROBE}`")
        for cmd in DENY_PROBES[:2]:
            if probe(hook, cmd.format(HOME=home), env) != 2:
                problems.append(f"the gate let `{cmd.replace('{HOME}', '~')}` through")
    now = snapshot(env)
    for key, what in (("deny", "deny rules"), ("denied", "must-deny probes stopped")):
        if now[key] < before.get(key, 0):
            problems.append(f"{what}: {now[key]}, fewer than the {before.get(key, 0)} before the swap")
    if session_check:
        try:
            ok = subprocess.run(shlex.split(session_check), timeout=PROBE_TIMEOUT_S * 6, check=False, env=dict(env))
        except (OSError, ValueError, subprocess.SubprocessError):
            problems.append("the session check could not run")
        else:
            if ok.returncode != 0:
                problems.append(f"the session check exits {ok.returncode}")
    return problems
