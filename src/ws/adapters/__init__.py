"""Runner adapters: one module per runner. To add a runner, add a module here; nothing else changes.

A module must define:

    NAME: str                                   the --runner name, e.g. "claude-code"
    def generate(policy, ws_cmd) -> dict        file name (relative to the out dir) -> text; pure, no I/O.
                                                `ws_cmd` is the argv list that runs ws, e.g. ["ws"].
    def parse(payload) -> Call | None           a hook payload from that runner, as a neutral Call; None if the call
                                                is not one the policy governs; ValueError or TypeError if it cannot be read
                                                (`ws gate check` then denies: fail closed).
    def launch_hint(out) -> str                 one line: how to start the runner with the generated config.

Modules are found by scanning this package, so the registry is the directory.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class Call:
    """A tool call in runner-neutral terms. kind: "command" (shell), "read" (file read) or "tool" (a neutral tool
    name from ws.policy.KNOWN_TOOLS). value: the command text, the path, or the tool name."""

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
