"""What every area shares: the view it is given, and the slot that describes it."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType

from ws.home.records import Snapshot


@dataclass(frozen=True)
class Hub:
    """What the page needs besides records, already worked out by `ws.home.hub` from the profile: whether a session can
    be started here, which asks can be answered here, and the choices behind the box's four pickers.

    `workspaces` holds `{name, cls}`; `machines` host names (this one first); `runners` holds `{name, models}` where each
    model is `{name, deny}` and `deny` the data classes it can't take. `classes` is workspace -> class.
    """

    start: bool = False
    answer_set: bool = False
    answerable: frozenset = frozenset()
    workspaces: tuple = ()
    machines: tuple = ()
    runners: tuple = ()
    classes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class View:
    """What an area renders from: the snapshot (records only), the hub, and whether a server stands behind the page.

    With `live` False (a rendered file) a call the server would run is shown as the line to paste instead of a button,
    and nothing can be started or answered.
    """

    snap: Snapshot
    live: bool = False
    hub: Hub = field(default_factory=Hub)


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
