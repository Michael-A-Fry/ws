"""Claude Code: a settings file with `permissions.deny` (the runner's own rules, the primary layer) plus a PreToolUse
hook that asks `ws gate check` (the second layer). `claude --bare` skips hooks, so the deny rules must stand alone;
the hook command ends `|| exit 2` so a missing or crashing ws blocks the call instead of letting it through (any
other hook failure is non-blocking in Claude Code).

Also denied: Edit/Write of the policy file and of the generated folder, so a session cannot loosen its own gate.

Pass the file with `claude --settings OUT/settings.json`. Nothing here touches ~/.claude.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from ws.adapters import Call, check_argv, protected_paths
from ws.policy import Policy, native_patterns

NAME = "claude-code"
FILE = "settings.json"
MATCHER = "Bash|Read|Grep|Glob|WebFetch|WebSearch|Edit|Write|MultiEdit|NotebookEdit"
_TOOLS = {"web_fetch": "WebFetch", "web_search": "WebSearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}


def _path(path: str) -> str:
    """Claude's path syntax: `~/x` is home, `//x` is an absolute path, anything else is cwd-relative gitignore."""
    return f"/{path}" if path.startswith("/") else path


def generate(policy: Policy, ws_cmd: list[str], out: Path, policy_file: Path) -> dict[str, str]:
    deny = [f"Bash({p})" for p in native_patterns(policy)]
    deny += [f"Read({_path(p)})" for p in policy.gate.deny_read]
    for p in protected_paths(out, policy_file):  # a file or a folder: cover both; Edit rules cover Write too
        deny += [f"Edit({_path(p)})", f"Edit({_path(p)}/**)"]
    deny += [_TOOLS[t] for t in policy.gate.deny_tools]
    hook = shlex.join(check_argv(ws_cmd, NAME, policy_file, out)) + " || exit 2"
    settings = {
        "permissions": {"deny": list(dict.fromkeys(deny))},
        "hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": hook}]}]},
    }
    return {FILE: json.dumps(settings, indent=2) + "\n"}


def parse(payload: dict) -> Call | list[Call] | None:
    """A PreToolUse payload: {"tool_name": "Bash", "tool_input": {"command": ...}, ...}."""
    name = payload.get("tool_name")
    inp = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(inp, dict):
        raise TypeError("payload has no tool_name and tool_input")
    if name == "Bash":
        return Call("command", _text(inp, "command"))
    if name == "Read":
        return Call("read", _text(inp, "file_path"))
    if name in ("Edit", "Write", "MultiEdit"):
        return Call("write", _text(inp, "file_path"))
    if name == "NotebookEdit":
        return Call("write", _text(inp, "notebook_path"))
    if name in ("Grep", "Glob"):
        calls = [Call("search", _optional(inp, "path") or ".")]
        pattern = inp.get("pattern")
        if name == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~")):
            calls.append(Call("read", pattern))  # an absolute glob names its own folder
        return calls
    if name in _NEUTRAL:
        return Call("tool", _NEUTRAL[name])
    return None


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"tool_input.{key} is missing")
    return v


def _optional(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is not None and not isinstance(v, str):
        raise TypeError(f"tool_input.{key} must be text")
    return v


def launch_hint(out: str) -> str:
    return f"start Claude Code with: claude --settings {shlex.quote(out)}/{FILE}"
