"""OpenCode: an `opencode.json` `permission` block (the runner's own rules, the primary layer) plus a shim plugin that
runs `ws gate check` on every governed tool call and throws on a deny (the second layer; it also fails closed: any
error in the shim blocks the call).

Point OpenCode at the output with two environment variables, so its global config is never touched:

    OPENCODE_CONFIG=OUT/opencode.json  OPENCODE_CONFIG_DIR=OUT

OPENCODE_CONFIG_DIR is searched for `plugins/`, which is where the shim sits.
"""

from __future__ import annotations

import json
import shlex
from importlib import resources

from ws.adapters import Call
from ws.policy import Policy, native_patterns

NAME = "opencode"
CONFIG = "opencode.json"
PLUGIN = "plugins/ws-gate.js"
_TOOLS = {"web_fetch": "webfetch", "web_search": "websearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}


def generate(policy: Policy, ws_cmd: list[str]) -> dict[str, str]:
    # Last matching rule wins, so the catch-all comes first.
    bash: dict[str, str] = {"*": "allow"}
    bash.update(dict.fromkeys(native_patterns(policy), "deny"))
    permission: dict = {"bash": bash}
    if policy.gate.deny_read:
        permission["read"] = {"*": "allow", **dict.fromkeys(policy.gate.deny_read, "deny")}
    for t in policy.gate.deny_tools:
        permission[_TOOLS[t]] = "deny"
    config = {"$schema": "https://opencode.ai/config.json", "permission": permission}
    plugin = resources.files("ws.adapters").joinpath("opencode_plugin.js").read_text(encoding="utf-8")
    plugin = plugin.replace("/*__WS_CMD__*/ []", json.dumps(ws_cmd))
    return {CONFIG: json.dumps(config, indent=2) + "\n", PLUGIN: plugin}


def parse(payload: dict) -> Call | None:
    """What the shim sends: {"tool": "bash", "args": {"command": ...}}."""
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise TypeError("payload has no tool and args")
    if tool in ("bash", "read"):
        key = "command" if tool == "bash" else "filePath"
        v = args.get(key)
        if not isinstance(v, str):
            raise TypeError(f"args.{key} is missing")
        return Call("command" if tool == "bash" else "read", v)
    if tool in _NEUTRAL:
        return Call("tool", _NEUTRAL[tool])
    return None


def launch_hint(out: str) -> str:
    q = shlex.quote(out)
    return f"start OpenCode with: OPENCODE_CONFIG={q}/{CONFIG} OPENCODE_CONFIG_DIR={q} opencode"
