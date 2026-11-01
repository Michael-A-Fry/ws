"""The home areas: one module per place on the page, each a `render(view) -> str` over records and nothing else.

`SLOTS` lists them in page order with what each reads, what it says when empty, how it treats an opaque item and what
a person can do there; `built` False is a slot that is registered and drawn once it exists. An area imports only
`ws.home.records`, `ws.ui` and `html` (a test reads the source to check), so it can't reach a file, a program or a
source: what it shows is what the API gave it.
"""

from __future__ import annotations

from ws.home.areas import evals, lead, needs_you, plugins, sessions, terminal
from ws.home.areas._base import Slot, View

__all__ = ["MODULES", "SLOTS", "Slot", "View", "built"]

MODULES = {m.ID: m for m in (lead, needs_you, sessions, evals, plugins, terminal)}
SLOTS: tuple[Slot, ...] = tuple(sorted((Slot.of(m) for m in MODULES.values()), key=lambda s: s.order))


def built() -> list[Slot]:
    """The slots the page draws, in order."""
    return [s for s in SLOTS if s.built]
