"""OpenCode: an `opencode.json` `permission` block (the runner's own rules, the primary layer) plus a shim plugin that
runs `ws gate check` on every governed tool call and throws on a deny (the second layer; it also fails closed: any
error in the shim blocks the call).

Also denied: edits of the policy file and of the generated folder, so a session cannot loosen its own gate.

Point OpenCode at the output so its global config is never touched, and so a project's own `opencode.json` cannot
loosen the gate: the inline config (OPENCODE_CONFIG_CONTENT) is read after the global, OPENCODE_CONFIG and project
files, so it wins.

    OPENCODE_CONFIG_CONTENT="$(cat OUT/opencode.json)"  OPENCODE_CONFIG_DIR=OUT  opencode

OPENCODE_CONFIG_DIR is searched for `plugins/`, which is where the shim sits.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from ws.adapters import Call, check_argv, protected_paths
from ws.adapters.opencode_plugin import SOURCE
from ws.policy import Policy, native_patterns

NAME = "opencode"
CONFIG = "opencode.json"
PLUGIN = "plugins/ws-gate.js"
_TOOLS = {"web_fetch": "webfetch", "web_search": "websearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}

# Where this runner keeps what ws installs globally, and where a repo may shadow it (read by ws.globals).
# Both the older singular and the newer plural folder names are listed. kinds: see adapters/claude_code.py.
GLOBALS = {
    "user_dir": "~/.config/opencode",
    "xdg": "opencode",
    "project_dir": ".opencode",
    "kinds": {
        "agents": "name",
        "agent": "name",
        "commands": "name",
        "command": "name",
        "plugins": "stem",
        "plugin": "stem",
    },
}


def _read_forms(rule: str) -> list[str]:
    """OpenCode matches read and edit rules against the path as it sees it (relative to the project, so a file in
    another folder arrives as `../../x/y`), and an absolute rule never matches that (tried on 1.19.4: `~/.ssh/**`
    and `/abs/.ssh/**` let a read through; `**/.ssh/**` blocks it). So each rule is also given as `**/` plus the
    path below home, or its last 3 segments if it is absolute. That over-blocks a same-named folder elsewhere,
    which is the safe side."""
    parts = rule.removeprefix("~/").lstrip("/").split("/")
    if rule.startswith("/"):
        parts = parts[-3:]
    return list(dict.fromkeys([rule, "**/" + "/".join(parts)]))


def _protect_forms(path: str, is_out: bool) -> list[str]:
    """Edit rules for a protected file or folder, in the same two spellings (see _read_forms). A folder name like
    `jobs` or `logs` is common, so a pattern never drops the parent folder: a ws control path keeps one
    (`.ws/logs/**`), the generated folder two (`.ws/gate/opencode/**`), whatever else is above it."""
    parts = Path(path).parts
    tail = "/".join(parts[-3:] if is_out else parts[-2:])
    return [path, f"{path}/**", f"**/{tail}", f"**/{tail}/**"]


def generate(policy: Policy, ws_cmd: list[str], out: Path, policy_file: Path) -> dict[str, str]:
    # Last matching rule wins, so the catch-all comes first.
    bash: dict[str, str] = {"*": "allow"}
    bash.update(dict.fromkeys(native_patterns(policy), "deny"))
    permission: dict = {"bash": bash}
    if policy.gate.deny_read:
        rules = {"*": "allow", **dict.fromkeys((f for r in policy.gate.deny_read for f in _read_forms(r)), "deny")}
        permission.update(read=rules, grep=dict(rules), glob=dict(rules), list=dict(rules))
    edit = {"*": "allow"}  # covers write and patch too; a path may be a file or a folder: cover both
    for p in protected_paths(out, policy_file):
        edit.update(dict.fromkeys(_protect_forms(p, p == str(out)), "deny"))
    permission["edit"] = edit
    for t in policy.gate.deny_tools:
        permission[_TOOLS[t]] = "deny"
    config = {"$schema": "https://opencode.ai/config.json", "permission": permission}
    plugin = SOURCE.replace("/*__CHECK__*/ []", json.dumps(check_argv(ws_cmd, NAME, policy_file, out)))
    plugin = plugin.replace("/*__WS__*/ []", json.dumps(ws_cmd))
    return {CONFIG: json.dumps(config, indent=2) + "\n", PLUGIN: plugin}


def parse(payload: dict) -> Call | None:
    """What the shim sends: {"tool": "bash", "args": {"command": ...}}."""
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise TypeError("payload has no tool and args")
    if tool == "bash":
        return Call("command", _text(args, "command"))
    if tool == "read":
        return Call("read", _text(args, "filePath"))
    if tool in ("edit", "write", "multiedit"):
        return Call("write", _text(args, "filePath"))
    if tool in ("grep", "glob", "list"):
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise TypeError("args.path must be text")
        return Call("search", path or ".")
    if tool in _NEUTRAL:
        return Call("tool", _NEUTRAL[tool])
    return None


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"args.{key} is missing")
    return v


def launch_hint(out: str) -> str:
    q = shlex.quote(out)
    return f'start OpenCode with: OPENCODE_CONFIG_CONTENT="$(cat {q}/{CONFIG})" OPENCODE_CONFIG_DIR={q} opencode'
