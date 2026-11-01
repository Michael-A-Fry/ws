"""Lead: the one session the profile's `[home] lead` names, pinned above everything else.

It is drawn from the same record as any session row, so there is one place to look for it. When it has an ask, the ask
shows under Needs you.
"""

from __future__ import annotations

from ws.home.areas import _fmt, _rows
from ws.home.areas._base import View
from ws.home.areas.sessions import session_row

ID = "lead"
ORDER = 1
TITLE = "Pinned"
READS = "lead: the one session `[home] lead` names"
EMPTY = "No lead session: set [home] lead"
OPAQUE = "The lead is always an open session, so nothing here is hidden."
ACTIONS = "Open it where it lives (the opener the profile picks)."
BUILT = True

BUCKET = {"waiting": "need", "working": "work", "ended": "done"}


def render(view: View) -> str:
    lead = view.snap.lead
    if lead is None:
        return f'<section class="grp" data-grp="pin"><h5>Pinned</h5><p class="more">{_fmt.esc(EMPTY)}</p></section>'
    bucket = BUCKET.get(lead.state, "idle")
    return _rows.group("pin", [session_row(view, lead, bucket, pin=True)])
