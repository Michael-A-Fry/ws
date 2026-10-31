"""Claude Code: a settings file with `permissions.deny` (the runner's own rules, the primary layer) plus a PreToolUse
hook on Bash that asks `ws gate check` (the second layer). `claude --bare` skips hooks, so the deny rules must stand
alone; the hook command ends `|| exit 2` so a missing or crashing ws blocks the call instead of letting it through
(any other hook failure is non-blocking in Claude Code).

Pass the file with `claude --settings OUT/settings.json`. Nothing here touches ~/.claude.
"""

from __future__ import annotations

import json
import shlex

from ws.adapters import Call
from ws.policy import Policy, native_patterns

NAME = "claude-code"
FILE = "settings.json"
_TOOLS = {"web_fetch": "WebFetch", "web_search": "WebSearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}


def _read_rule(path: str) -> str:
    """Claude's path syntax: `~/x` is home, `//x` is an absolute path, anything else is cwd-relative gitignore."""
    return f"Read(/{path})" if path.startswith("/") else f"Read({path})"


def generate(policy: Policy, ws_cmd: list[str]) -> dict[str, str]:
    deny = [f"Bash({p})" for p in native_patterns(policy)]
    deny += [_read_rule(p) for p in policy.gate.deny_read]
    deny += [_TOOLS[t] for t in policy.gate.deny_tools]
    hook = f"{shlex.join(ws_cmd)} gate check --runner {NAME} || exit 2"
    settings = {
        "permissions": {"deny": list(dict.fromkeys(deny))},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook}]}]},
    }
    return {FILE: json.dumps(settings, indent=2) + "\n"}


def parse(payload: dict) -> Call | None:
    """A PreToolUse payload: {"tool_name": "Bash", "tool_input": {"command": ...}, ...}."""
    name = payload.get("tool_name")
    inp = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(inp, dict):
        raise TypeError("payload has no tool_name and tool_input")
    if name == "Bash":
        return Call("command", _text(inp, "command"))
    if name == "Read":
        return Call("read", _text(inp, "file_path"))
    if name in _NEUTRAL:
        return Call("tool", _NEUTRAL[name])
    return None


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"tool_input.{key} is missing")
    return v


def launch_hint(out: str) -> str:
    return f"start Claude Code with: claude --settings {shlex.quote(out)}/{FILE}"
