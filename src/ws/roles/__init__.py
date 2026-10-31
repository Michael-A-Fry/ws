"""Role adapters: one module per (role, adapter) pair. To add one, add a module here; nothing else changes.

A module must define:

    ROLE: str          the doctor role it serves: model, secrets, memory, scheduler, notify or backup
    NAME: str          the `adapter = "..."` value in [roles.<role>] of the profile
    def check(cfg, osi, env, network) -> (status, detail)
                       status is ok, warn or fail; the detail never holds a secret
    def steps(cfg, answers, env) -> list[install.Step]
                       the files and folders this adapter needs, for `install.apply`; pure, writes nothing

An adapter may add more (the schedulers have `activate` and `deactivate`). Modules are found by scanning this package,
like ws.adapters, so the registry is the directory. A module whose name starts with an underscore is a helper.
`ws doctor` picks the adapter from the profile and calls its check; it holds no list of names.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from types import ModuleType

OK, WARN, FAIL = "ok", "warn", "fail"


def all_roles() -> dict[str, dict[str, ModuleType]]:
    """{role: {adapter name: module}}, both sorted."""
    found: dict[str, dict[str, ModuleType]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name == "commands":
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, "ROLE") and hasattr(mod, "NAME") and hasattr(mod, "check"):
            found.setdefault(mod.ROLE, {})[mod.NAME] = mod
    return {r: dict(sorted(a.items())) for r, a in sorted(found.items())}


def adapters(role: str) -> dict[str, ModuleType]:
    return all_roles().get(role, {})


def get(role: str, name: str) -> ModuleType:
    known = adapters(role)
    if name not in known:
        raise KeyError(f"unknown {role} adapter {name!r} (known: {', '.join(known)})")
    return known[name]


def check(role: str, cfg: dict, osi, env, network: bool, default: str | None = None) -> tuple[str, str]:
    """The check of the adapter [roles.<role>] names (or `default` if it names none)."""
    known = adapters(role)
    a = cfg.get("adapter", default)
    if not isinstance(a, str) or not a:
        return FAIL, "no adapter named"
    if a not in known:
        return FAIL, f"unknown adapter {a!r}; known: {', '.join(sorted(known))}"
    return known[a].check(cfg, osi, env, network)


def folder(cfg: dict) -> tuple[Path | None, tuple[str, str] | None]:
    """The folder a role's `path` names: (folder, None), or (None, (status, why not))."""
    p = cfg.get("path")
    if not isinstance(p, str) or not p:
        return None, (FAIL, "no path set")
    d = Path(p).expanduser()
    return (d, None) if d.is_dir() else (None, (FAIL, f"{d} is not a folder"))


def register(sub) -> None:
    """`ws notify`, `ws backup` and `ws schedule`."""
    from ws.roles import commands

    commands.register(sub)
