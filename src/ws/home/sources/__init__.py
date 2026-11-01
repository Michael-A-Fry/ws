"""Home sources: one module per place the home data comes from. To add one, add a module here; nothing else changes.

A module must define:

    NAME: str          the name shown in `sources[]` of the snapshot
    PROVIDES: tuple    the kinds it reads: sessions, asks, events, plugins, evals
    ORDER: int         where it reads among the others (low first; a later one may use what an earlier one found)
    def available(env) -> (ok, why)
                       cheap: is there anything here to read? `why` says what is missing
    def read(kind, env) -> list
                       the records of one kind (ws.home.records), whole or by raising SourceError

Modules are found by scanning this package, like ws.roles, so the registry is the directory. A module whose name starts
with an underscore is a helper. The API calls `available` once, then `read` for each kind in PROVIDES; if anything
raises, none of that source's records are used and it becomes one `sources[]` row with a typed reason. A source never
opens a file ending in `.key` and never reads a transcript beyond its `usage` fields; the helpers in `_files.py` are the
only way the sources open a file.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

KINDS = ("sessions", "asks", "events", "plugins", "evals")
# Why a source gave nothing, as a word the page can branch on.
UNAVAILABLE, UNREADABLE, BAD_FORMAT, TIMEOUT, FAILED = "unavailable", "unreadable", "bad_format", "timeout", "failed"
REASONS = (UNAVAILABLE, UNREADABLE, BAD_FORMAT, TIMEOUT, FAILED)


class SourceError(Exception):
    """A source could not be read. `kind` is one of REASONS; `why` is one plain sentence with no file contents."""

    def __init__(self, kind: str, why: str) -> None:
        super().__init__(why)
        self.kind = kind
        self.why = why


def all_sources() -> list[ModuleType]:
    """Every source module, in the order they read."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if all(hasattr(mod, a) for a in ("NAME", "PROVIDES", "ORDER", "available", "read")):
            found.append(mod)
    return sorted(found, key=lambda m: (m.ORDER, m.NAME))
