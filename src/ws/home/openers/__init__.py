"""Home openers: one module per way to open a session or an ask where it lives. To add one, add a module here.

A module must define:

    NAME: str          the name a profile's `[home.open]` uses
    ORDER: int         where it comes among the others when the profile picks none (low first)
    OPAQUE_OK: bool    True when it may be offered for an opaque record (one that needs nothing the record hides)
    def available(ctx) -> (ok, why)
                       cheap: can this opener work here at all? `why` says what is missing
    def action(rec, ctx) -> dict | None
                       what to offer for this record, or None when it doesn't apply. A link is
                       `{"label", "href"}`; a call the page's server runs is `{"label", "post": True, "text"}`, where
                       `text` is the same thing as a line to paste (shown when the page has no server behind it); a
                       line to paste alone is `{"label", "text"}`
    def argv(rec, ctx) -> list[str] | None     (only for a module that offers `post`)
                       the program and arguments the server runs: no shell, after the token, Host and Origin checks

Modules are found by scanning this package, like ws.roles, so the registry is the directory. A module whose name
starts with an underscore is a helper. `ctx` is `Ctx`: the environment and the `[home.open]` argument for the opener.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import ModuleType

__all__ = ["Ctx", "discover"]


@dataclass(frozen=True)
class Ctx:
    """What an opener reads: the environment, and the text after `command:` in the profile's `[home.open]`."""

    environ: Mapping[str, str] = field(default_factory=dict)
    arg: str = ""


def discover() -> list[ModuleType]:
    """Every opener module, in the order they come when nothing is picked. One that won't import is left out."""
    found: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:  # noqa: BLE001, S112 - an opener that won't import is one fewer way to open, not a crash
            continue
        if all(hasattr(mod, a) for a in ("NAME", "ORDER", "OPAQUE_OK", "available", "action")):
            found.append(mod)
    return sorted(found, key=lambda m: (m.ORDER, m.NAME))
