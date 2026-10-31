"""Named stub-model scenarios, shared by the end-to-end tests and the eval: what the fake provider asks a runner to do,
and the outcome a correct ws gives.

A scenario is `{description, calls, final_text, expect}`:

  * `calls` are the tool calls the stub makes in order. Each names its tool per runner (`"tool": {"claude-code": "Bash",
    "opencode": "bash"}`; the runners spell them differently) and gives `args` per runner. `${work}` in a string is the
    project folder, `${...}` anything else the caller passes to `calls_for`.
  * `final_text` is what the stub says once the calls are done (default "ok, done").
  * `expect` is the outcome, as data a test or the eval reads: `outcome` is one of `blocked` (every call was refused),
    `executed` (the calls ran), `refused` (ws refused before the runner started: exit 3, no request reached the stub) or
    `text_only` (no tool was called); `present` and `absent` are paths that must or must not exist afterwards;
    `tool_results` is how many results the runner should have reported back; `stub_requests` the number of requests the
    stub should have seen.

`tests/scenarios.json` is the file to edit and read. `DEFAULT` is the same set as Python, for a ws installed outside a
checkout (`ws secret wall` copies only `.py` files); a test keeps the two equal. `WS_MOCK_SCENARIOS` or a path argument
loads another file in the same shape.
"""

from __future__ import annotations

import json
import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from ws.testing.mockprovider import Call

SCHEMA = 1
RUNNERS = ("claude-code", "opencode")
PROVIDERS = {"anthropic": "claude-code", "openai": "opencode"}  # which wire format each runner speaks
OUTCOMES = ("blocked", "executed", "refused", "text_only")

_DENIED = "touch ${work}/DENIED && git push --force nowhere main"
_ALLOWED = "touch ${work}/ALLOWED && git status"

DEFAULT: dict = {
    "schema": SCHEMA,
    "scenarios": {
        "denied_command": {
            "description": "The model asks for a command the default policy denies (it would also make a marker file). The gate blocks it and the marker never appears.",
            "calls": [
                {
                    "tool": {"claude-code": "Bash", "opencode": "bash"},
                    "args": {
                        "claude-code": {"command": _DENIED},
                        "opencode": {"command": _DENIED, "description": "run it"},
                    },
                }
            ],
            "expect": {"outcome": "blocked", "tool_results": 1, "absent": ["${work}/DENIED"]},
        },
        "approved_command": {
            "description": "A command the policy lets through once approved (or allows outright). It runs, and the marker appears.",
            "calls": [
                {
                    "tool": {"claude-code": "Bash", "opencode": "bash"},
                    "args": {
                        "claude-code": {"command": _ALLOWED},
                        "opencode": {"command": _ALLOWED, "description": "run it"},
                    },
                }
            ],
            "expect": {"outcome": "executed", "tool_results": 1, "present": ["${work}/ALLOWED"]},
        },
        "broker_lease": {
            "description": "A harmless command under a `ws run` with a leased key. The stub sees the real key the broker injects, the runner only a placeholder, and the lease is released.",
            "calls": [
                {
                    "tool": {"claude-code": "Bash", "opencode": "bash"},
                    "args": {
                        "claude-code": {"command": "echo hi"},
                        "opencode": {"command": "echo hi", "description": "run it"},
                    },
                }
            ],
            "expect": {"outcome": "executed", "tool_results": 1},
        },
        "budget_refusal": {
            "description": "The month's spend is at the cap. ws refuses the run (exit 3) before the runner starts, so the stub is never called.",
            "calls": [],
            "expect": {"outcome": "refused", "stub_requests": 0},
        },
        "first_task": {
            "description": "The harmless first task the install-to-first-task timing uses: one call that writes a file in the project, then the model finishes.",
            "calls": [
                {
                    "tool": {"claude-code": "Edit", "opencode": "write"},
                    "args": {
                        "claude-code": {"file_path": "${work}/hello.txt", "old_string": "", "new_string": "hello"},
                        "opencode": {"filePath": "${work}/hello.txt", "content": "hello"},
                    },
                }
            ],
            "final_text": "I wrote hello.txt.",
            "expect": {"outcome": "executed", "tool_results": 1, "present": ["${work}/hello.txt"]},
        },
        "plain_text": {
            "description": "The model answers in text and calls no tool.",
            "calls": [],
            "final_text": "ok, done",
            "expect": {"outcome": "text_only", "tool_results": 0},
        },
    },
}


class ScenarioError(Exception):
    """An unknown scenario, or a file that is not in the shape above."""


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    calls: tuple[dict, ...]
    final_text: str = "ok, done"
    expect: dict = field(default_factory=dict)

    def calls_for(self, runner: str, **variables: str) -> list[Call]:
        """The stub's script for `runner` ("claude-code" or "opencode"), with `${name}` filled in from `variables`."""
        if runner not in RUNNERS:
            raise ScenarioError(f"runner must be one of {', '.join(RUNNERS)}, not {runner!r}")
        out = []
        for i, c in enumerate(self.calls, 1):
            if runner not in c["tool"] or runner not in c["args"]:
                raise ScenarioError(f"scenario {self.name}: call {i} has no tool or args for {runner}")
            out.append(Call(c["tool"][runner], fill(c["args"][runner], variables)))
        return out

    def expectation(self, **variables: str) -> dict:
        """`expect` with `${name}` filled in (the paths)."""
        return fill(self.expect, variables)


def fill(value, variables: dict):
    """`value` with `${name}` replaced in every string; a name left out stays as written."""
    if isinstance(value, str):
        return string.Template(value).safe_substitute(variables)
    if isinstance(value, dict):
        return {k: fill(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, variables) for v in value]
    return value


def problems(doc) -> list[str]:
    """What is wrong with a scenarios document (an empty list when it is fine)."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("scenarios"), dict):
        return [f"the file must be an object with schema {SCHEMA} and a scenarios table"]
    out = []
    for name, s in doc["scenarios"].items():
        where = f"scenario {name}"
        if not isinstance(s, dict):
            out.append(f"{where}: must be an object")
            continue
        extra = set(s) - {"description", "calls", "final_text", "expect"}
        if extra:
            out.append(f"{where}: unknown key {min(extra)!r}")
        if not isinstance(s.get("description"), str) or not s["description"]:
            out.append(f"{where}: description must be text")
        if not isinstance(s.get("calls"), list):
            out.append(f"{where}: calls must be a list")
        for i, c in enumerate(s["calls"] if isinstance(s.get("calls"), list) else [], 1):
            if not (isinstance(c, dict) and set(c) == {"tool", "args"}):
                out.append(f"{where}: call {i} must have exactly tool and args")
                continue
            for key in ("tool", "args"):
                if not isinstance(c[key], dict) or set(c[key]) != set(RUNNERS):
                    out.append(f"{where}: call {i} {key} must be given for {' and '.join(RUNNERS)}")
            if isinstance(c["tool"], dict) and not all(isinstance(t, str) and t for t in c["tool"].values()):
                out.append(f"{where}: call {i} tool names must be text")
            if isinstance(c["args"], dict) and not all(isinstance(a, dict) for a in c["args"].values()):
                out.append(f"{where}: call {i} args must be objects")
        if "final_text" in s and not isinstance(s["final_text"], str):
            out.append(f"{where}: final_text must be text")
        e = s.get("expect")
        if not isinstance(e, dict) or e.get("outcome") not in OUTCOMES:
            out.append(f"{where}: expect.outcome must be one of {', '.join(OUTCOMES)}")
        elif not all(isinstance(e.get(k, []), list) for k in ("present", "absent")):
            out.append(f"{where}: expect.present and expect.absent must be lists of paths")
    return out


def load(path: str | Path | None = None, env: dict | None = None) -> dict[str, Scenario]:
    """Every scenario by name: from `path`, else `$WS_MOCK_SCENARIOS`, else the built-in set."""
    env = os.environ if env is None else env
    chosen = path or env.get("WS_MOCK_SCENARIOS")
    if chosen:
        try:
            doc = json.loads(Path(chosen).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ScenarioError(f"cannot read scenarios from {chosen}: {type(e).__name__}") from e
    else:
        doc = DEFAULT
    bad = problems(doc)
    if bad:
        raise ScenarioError(bad[0])
    return {
        name: Scenario(name, s["description"], tuple(s["calls"]), s.get("final_text", "ok, done"), s["expect"])
        for name, s in doc["scenarios"].items()
    }


def get(name: str, path: str | Path | None = None, env: dict | None = None) -> Scenario:
    found = load(path, env)
    if name not in found:
        raise ScenarioError(f"no scenario {name!r} (known: {', '.join(found)})")
    return found[name]
