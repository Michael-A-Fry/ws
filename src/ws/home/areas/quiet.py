"""The quiet line at the foot of the list: Scheduled, Evals (the last verdict in words) and Tidy, each a link.

It says what is behind each link and one fact where the records have one: the eval verdict, and how many ended sessions
still hold a tmux window. Nothing else on Home reports on evals or tidiness, so it stays one line.
"""

from __future__ import annotations

from ws.home.areas import _fmt, _rows, evals
from ws.home.areas._base import View

ID = "quiet"
ORDER = 7
TITLE = "Quiet line"
READS = "evals (the last verdict) and sessions (ended ones that still hold a window)"
EMPTY = "no eval trials yet"
OPAQUE = "It counts ended sessions and reads no name or folder, so nothing is hidden."
ACTIONS = "Follow a link to the page behind it."
BUILT = True


def _link(page: str, label: str, text: str) -> str:
    return f'<a href="#v-{page}"><b>{label}</b> {_fmt.esc(text)} ›</a>'


def render(view: View) -> str:
    ended = _rows.buckets(view.snap).ended
    windows = f"{ended} ended session window{'s' if ended != 1 else ''}" if ended else "nothing to clear"
    return (
        '<div class="quiet" id="quiet">'
        + _link("scheduled", "Scheduled", "jobs, their next run and last result")
        + _link("evals", "Evals", evals.verdict(view.snap.evals, view.snap.ts))
        + _link("history", "Tidy", windows)
        + "</div>"
    )
