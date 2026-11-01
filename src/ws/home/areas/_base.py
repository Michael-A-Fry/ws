"""What every area shares: the view it is given, and the slot that describes it."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from ws.home.records import Snapshot


@dataclass(frozen=True)
class View:
    """What an area renders from: the snapshot (records only), and whether a server stands behind the page.

    With `live` False (a rendered file) a call the server would run is shown as the line to paste instead of a button.
    """

    snap: Snapshot
    live: bool = False


@dataclass(frozen=True)
class Slot:
    """One place on the home page: where it sits, what it reads, what it says when empty, how it treats an opaque item
    and what a person can do there. `built` False is a slot that is registered and waits to be drawn."""

    id: str
    order: int
    title: str
    reads: str
    empty: str
    opaque: str
    actions: str
    built: bool

    @classmethod
    def of(cls, mod: ModuleType) -> Slot:
        return cls(mod.ID, mod.ORDER, mod.TITLE, mod.READS, mod.EMPTY, mod.OPAQUE, mod.ACTIONS, mod.BUILT)
