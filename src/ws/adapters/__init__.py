"""Runner adapters: one module per runner. To add a runner, add a module here; nothing else changes.

A module must define:

    NAME: str                                   the --runner name, e.g. "claude-code"
    def generate(policy, ws_cmd, out, policy_file) -> dict
                                                file name (relative to the out dir) -> text; pure, no I/O.
                                                `ws_cmd` is the argv list that runs ws, e.g. ["ws"]; `out` is the
                                                folder the files go in and `policy_file` the policy they came from:
                                                both are ws's own files, so the runner must not let a session edit
                                                them (see check_argv for the hook's side of that).
    def parse(payload) -> Call | list[Call] | None
                                                a hook payload from that runner, as neutral Call(s); None if the call
                                                is not one the policy governs; ValueError or TypeError if it cannot be
                                                read (`ws gate check` then denies: fail closed).
    def launch_hint(out) -> str                 one line: how to start the runner with the generated config.

Modules are found by scanning this package, so the registry is the directory.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


@dataclass(frozen=True)
class Call:
    """A tool call in runner-neutral terms. kind: "command" (shell), "read" (one file read), "search" (a folder
    searched or listed, recursively), "write" (a file written or edited) or "tool" (a neutral tool name from
    ws.policy.KNOWN_TOOLS). value: the command text, the path, or the tool name."""

    kind: str
    value: str


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


def protected_paths(out: Path, policy_file: Path) -> list[str]:
    """Everything a session must not write: the generated folder, the policy, and ws's other control files next to
    it (profile, secrets, jobs, release, runs, logs). Each adapter denies edits to all of them natively."""
    from ws.policy import control_paths

    paths = [str(out), str(policy_file), *control_paths(policy_file.parent)]
    return list(dict.fromkeys(paths))


def check_argv(ws_cmd: list[str], runner: str, policy_file: Path, out: Path) -> list[str]:
    """The command a generated hook or shim runs. The policy and the protected folder are absolute paths, so the hook
    reads the same policy wherever the runner starts and cannot be pointed at another one by the session's cwd."""
    return [*ws_cmd, "gate", "check", "--runner", runner, "--policy", str(policy_file), "--protect", str(out)]
