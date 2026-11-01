"""Needs you: everything waiting for a person, oldest first: a question, a permission, an approval, a draft.

Each item names its source and the session it came from, shows the recommended option first and says where it is
answered (the app or a terminal). An opaque item shows only its kind, folder label and tmux target, says to open the
session to see it, and is offered only an opener that is safe for that.
"""

from __future__ import annotations

from ws.home.areas import _fmt
from ws.home.areas._base import View
from ws.ui import components

ID = "needs_you"
ORDER = 2
TITLE = "Needs you"
READS = "asks: what is waiting for the person, oldest first"
EMPTY = "Nothing is waiting for you."
OPAQUE = "Only the kind, folder label and tmux target show; the line says to open the session to see this."
ACTIONS = "Open the session or ask where it is answered; answering on the page comes later."
BUILT = True

KINDS = {
    "question": "Question",
    "permission": "Permission",
    "approval": "Approval",
    "waiting": "Waiting",
    "draft": "Draft",
    "plugin": "Plugin",
}
OPTIONS_SHOWN = 4


def _options(ask) -> str:
    if not ask.options:
        return ""
    items = []
    for i, o in enumerate(ask.options[:OPTIONS_SHOWN]):
        tag = '<span class="rec">recommended</span>' if i == 0 and len(ask.options) > 1 else ""
        desc = f'<span class="d">{_fmt.esc(o.get("description", ""))}</span>' if o.get("description") else ""
        items.append(f"<li><b>{_fmt.esc(o.get('label', ''))}</b>{tag}{desc}</li>")
    return f'<ul class="opts">{"".join(items)}</ul>'


def item(view: View, ask) -> str:
    kind = KINDS.get(ask.kind, ask.kind.capitalize())
    where_in = "Answer in the app" if ask.answer_in == "app" else "Answer in a terminal"
    if ask.opaque:
        title, who = _fmt.OPAQUE_LINE, ask.repo
    else:
        title = ask.title or f"{kind}: no title given"
        who = ask.session_name or ask.repo
    meta = [m for m in (who, ask.tmux, ask.source, _fmt.age(ask.created, view.snap.ts), where_in) if m]
    chips = "".join(components.chip(m) for m in meta)
    return (
        '<li class="ask">'
        f'<div class="c-st">{components.status("waiting", kind)}</div>'
        f'<div class="c-who"><b class="nm">{_fmt.esc(title)}</b><span class="meta">{chips}</span>'
        f"{'' if ask.opaque else _options(ask)}</div>"
        f'<div class="c-open">{_fmt.opens(view, ask, "ask")}</div></li>'
    )


def render(view: View) -> str:
    asks = view.snap.asks
    if not asks:
        return components.panel(TITLE, components.empty(EMPTY), aside="all clear")
    body = f'<ul class="list">{"".join(item(view, a) for a in asks)}</ul>'
    return components.panel(TITLE, body, aside=f"{len(asks)} waiting")
