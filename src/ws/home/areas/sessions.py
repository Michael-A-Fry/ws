"""Sessions: every session the sources found, by host, with its state, where it runs, its tokens and what it cost.

Live ones come first; the ones that have ended are folded into one line (a count, and the newest few) so a long history
doesn't push the rest down. An opaque session shows its folder label, tmux target and state, and says to open the
session to see more; its tokens and cost read `hidden`.
"""

from __future__ import annotations

from ws.home.areas import _fmt
from ws.home.areas._base import View
from ws.ui import components

ID = "sessions"
ORDER = 3
TITLE = "Sessions"
READS = "sessions: the records of every session, by host"
EMPTY = "No sessions found on this machine yet."
OPAQUE = "Name, folder, tokens and cost are hidden; the line says to open the session to see this."
ACTIONS = "Open the session where it lives (the opener the profile picks)."
BUILT = True

ENDED_SHOWN = 8


def name_of(s) -> str:
    if s.opaque:
        return "Hidden session"
    return s.name or s.id[:8]


def row(view: View, s) -> str:
    """One session as a list item: state, name and where, tokens and cost, then the ways to open it."""
    where = [p for p in ((s.repo if s.opaque else s.host), s.tmux, "" if s.opaque else s.cwd) if p]
    note = ""
    if s.opaque:
        note = f'<span class="note">{_fmt.OPAQUE_LINE}</span>'
    elif s.state == "waiting" and s.waiting_for:
        note = f'<span class="note">Waiting for: {_fmt.esc(s.waiting_for)}</span>'
    seen = _fmt.age(s.last_seen, view.snap.ts)
    seen = "" if seen == "just now" else seen
    return (
        f'<li class="sess{" lead" if s.lead else ""}">'
        f'<div class="c-st">{components.status(s.state)}</div>'
        f'<div class="c-who"><b class="nm">{_fmt.esc(name_of(s))}</b>'
        f'<span class="where">{_fmt.esc(" · ".join(where))}</span>{note}</div>'
        f'<div class="c-num"><span>{_fmt.esc(_fmt.tokens(s))}</span><span>{_fmt.esc(_fmt.cost(s))}</span>'
        f'<span class="seen">{_fmt.esc(seen)}</span></div>'
        f'<div class="c-open">{_fmt.opens(view, s, "session")}</div></li>'
    )


def _host(view: View, name: str, items: list) -> str:
    head = (
        f'<h3 class="host">{_fmt.esc(name or "this machine")}</h3>'
        if len({s.host for s in view.snap.sessions}) > 1
        else ""
    )
    return head + f'<ul class="list">{"".join(row(view, s) for s in items)}</ul>'


def render(view: View) -> str:
    pool = [s for s in view.snap.sessions if not s.lead]
    if not pool and view.snap.lead is None:
        return components.panel(TITLE, components.empty(EMPTY))
    live = [s for s in pool if s.state != "ended"]
    ended = [s for s in pool if s.state == "ended"]
    hosts = list(dict.fromkeys(s.host for s in live))
    body = "".join(_host(view, h, [s for s in live if s.host == h]) for h in hosts)
    if not live:
        body = components.empty("Nothing is running right now.")
    if ended:
        shown = ended[:ENDED_SHOWN]
        more = len(ended) - len(shown)
        tail = f'<p class="empty">and {more} more</p>' if more > 0 else ""
        body += (
            f'<details class="fold"><summary>{len(ended)} ended</summary>'
            f'<ul class="list">{"".join(row(view, s) for s in shown)}</ul>{tail}</details>'
        )
    aside = f"{len(live)} live" if live else "none live"
    return components.panel(TITLE, body, aside=aside)
