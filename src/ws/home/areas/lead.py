"""Lead: the one session the profile's `[home] lead` names, pinned at the top with a rule under it.

It is drawn from the same record as a row in Sessions (which leaves it out), so there is one place to look for it.
"""

from __future__ import annotations

from ws.home.areas import _fmt
from ws.home.areas._base import View
from ws.home.areas.sessions import row

ID = "lead"
ORDER = 1
TITLE = "Lead"
READS = "lead: the one session `[home] lead` names"
EMPTY = "No lead session: set [home] lead"
OPAQUE = "The lead is always an open session, so nothing here is hidden."
ACTIONS = "Open it where it lives (the opener the profile picks)."
BUILT = True


def render(view: View) -> str:
    lead = view.snap.lead
    if lead is None:
        return f'<section class="lead"><p class="empty">{_fmt.esc(EMPTY)}</p></section><hr class="sep">'
    return (
        '<section class="lead"><h2 class="lead-h">Lead</h2>'
        f'<ul class="list">{row(view, lead)}</ul></section><hr class="sep">'
    )
