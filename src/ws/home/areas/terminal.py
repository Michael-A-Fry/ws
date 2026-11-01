"""Terminal: a terminal inside the page for one session. A slot: registered here, drawn once it is built.

It opens for an opaque item only when the person owns the session it belongs to.
"""

from __future__ import annotations

from ws.home.areas._base import View

ID = "terminal"
ORDER = 6
TITLE = "Terminal"
READS = "sessions: the one the person picks"
EMPTY = "Pick a session to open its terminal."
OPAQUE = "Only for an opaque item whose owner is checked."
ACTIONS = "Type into the session's terminal."
BUILT = False


def render(view: View) -> str:
    return ""
