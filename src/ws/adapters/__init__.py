"""Runner adapters: one module per runner. To add a runner, add a module here; nothing else changes.

A module must define:

    NAME: str                                   the --runner name, e.g. "claude-code"
    def generate(policy, ws_cmd, out, policy_file, *, protect=()) -> dict
                                                file name (relative to the out dir) -> text; pure, no I/O.
                                                `ws_cmd` is the argv list that runs ws, e.g. ["ws"]; `out` is the
                                                folder the files go in and `policy_file` the policy they came from:
                                                both are ws's own files, so the runner must not let a session edit
                                                them (see check_argv for the hook's side of that). `protect` names
                                                more paths a session may not write (deny rules, the hook's
                                                --protect arguments, the sandbox's denyWrite).
    def parse(payload) -> Call | list[Call] | None
                                                a hook payload from that runner, as neutral Call(s); None if the call
                                                is not one the policy governs; ValueError or TypeError if it cannot be
                                                read (`ws gate check` then denies: fail closed).
    def launch_hint(out) -> str                 one line: how to start the runner with the generated config.

and, optionally:

    UNSUPPORTED_GUARDS: tuple[str, ...]         guard names (ws.guards) this runner's generated config does not run.
    EVENT_NAMES: dict[str, str]                 the runner's hook event name -> a ws.guards event ("pre", "post",
                                                "start", "prompt", "stop", "subagent-stop"), for `ws gate event`.

Modules are found by scanning this package, so the registry is the directory.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType


@dataclass(frozen=True)
class Call:
    """A tool call in runner-neutral terms. kind: "command" (shell), "read" (one file read), "search" (a folder
    searched or listed, recursively), "write" (a file written or edited), "tool" (a neutral tool name from
    ws.policy.KNOWN_TOOLS), "agent" (a subagent started), "skill" (a skill loaded) or "message" (a message sent to
    another agent). value: the command text, the path, the tool name, the agent type, the skill name or the
    recipient. data: what else the tool gave, read-only: a write has `old`, `new`, `content` or `edits` as the tool
    supplies them; an agent has `prompt`, `description` and `subagent_type`; a skill has `name`; a message has `text`.
    The policy judges command, read, search, write and tool calls; the other kinds reach guards only."""

    kind: str
    value: str
    data: Mapping = field(default_factory=lambda: MappingProxyType({}), hash=False)

    @classmethod
    def of(cls, kind: str, value: str, **data) -> Call:
        """A Call with its `data` set (read-only)."""
        return cls(kind, value, MappingProxyType(data))


def all_adapters() -> dict[str, ModuleType]:
    found = {}
    for info in pkgutil.iter_modules(__path__):
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, "NAME") and hasattr(mod, "generate"):
            found[mod.NAME] = mod
    return dict(sorted(found.items()))


def get(name: str) -> ModuleType:
    adapters = all_adapters()
    if name not in adapters:
        raise KeyError(f"unknown runner {name!r} (known: {', '.join(adapters)})")
    return adapters[name]


def protected_paths(out: Path, policy_file: Path, extra: tuple[str, ...] = ()) -> list[str]:
    """Everything a session must not write: the generated folder, the policy, ws's other control files next to
    it (profile, secrets, jobs, release, runs, logs), and any `extra` path the caller names (a user settings file).
    Each adapter denies edits to all of them natively."""
    from ws.policy import control_paths

    paths = [str(out), str(policy_file), *control_paths(policy_file.parent), *extra]
    return list(dict.fromkeys(paths))


def check_argv(
    ws_cmd: list[str], runner: str, policy_file: Path, out: Path, protect: tuple[str, ...] = ()
) -> list[str]:
    """The command a generated hook or shim runs. The policy and the protected folder are absolute paths, so the hook
    reads the same policy wherever the runner starts and cannot be pointed at another one by the session's cwd. Each
    `protect` path is one more `--protect`."""
    argv = [*ws_cmd, "gate", "check", "--runner", runner, "--policy", str(policy_file), "--protect", str(out)]
    for p in protect:
        argv += ["--protect", str(p)]
    return argv


def event_argv(ws_cmd: list[str], runner: str, policy_file: Path) -> list[str]:
    """The command a generated hook runs for the events that are not a tool call's check: `ws gate event`."""
    return [*ws_cmd, "gate", "event", "--runner", runner, "--policy", str(policy_file)]
