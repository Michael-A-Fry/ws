"""The home page: a sidebar of places and one quiet column, from a snapshot and nothing else.

`render(snap, nonce, live=..., hub=...)` returns the whole page as one string. Home is the box to start work in, then the
list: Pinned (the lead), Needs you, Working, Idle (folded) and Done today, then a quiet line of Scheduled, Evals and Tidy.
Evals and Sources are pages of their own, one level down; the other places in the sidebar say what is coming. It reads
only the records and hub it is given (and `ws.ui`), so a page drawn from a file shows what a page served on loopback
shows. Rendering changes nothing.
"""

from __future__ import annotations

from ws.home import areas
from ws.home.areas import _chrome, _fmt, _rows, _script, _style
from ws.home.records import Snapshot, SourceRow
from ws.ui import components

__all__ = ["CSS", "SCRIPT", "SCRIPT_LIVE", "render"]

CSS = _style.CSS
SCRIPT = _script.SHARED
SCRIPT_LIVE = _script.SHARED + _script.LIVE

_SOURCE_STATUS = {"unavailable": ("idle", "not here")}


def _source(row: SourceRow) -> str:
    if row.ok:
        mark = components.status("ok", "read")
        why = f"{row.n} record{'s' if row.n != 1 else ''}"
    else:
        state, word = _SOURCE_STATUS.get(row.kind, ("warn", row.kind.replace("_", " ")))
        mark, why = components.status(state, word), row.why
    return f'<li><span class="nm">{_fmt.esc(row.name)}</span>{mark}<span class="why">{_fmt.esc(why)}</span></li>'


def _sources(snap: Snapshot) -> str:
    """The sources that were read, folded: a person looks here only when a number on the page looks wrong."""
    rows = snap.sources
    bad = sum(1 for r in rows if not r.ok and r.kind != "unavailable")
    aside = f"{len(rows)} read" if not bad else f"{bad} need a look"
    body = f'<ul class="list srcs">{"".join(_source(r) for r in rows)}</ul>'
    return (
        '<section class="panel"><details><summary data-action="sources.toggle" data-expect="toggle:details">Sources'
        f"<span>{_fmt.esc(aside)}</span></summary>{body}</details></section>"
    )


def _summary(snap: Snapshot) -> str:
    """One count line: how many need you, are working, idle or done today."""
    b = _rows.buckets(snap)
    parts = [
        f"{b.need} need you" if b.need else "nothing needs you",
        f"{len(b.work)} working",
        f"{len(b.idle)} idle",
        f"{b.done_today} done today",
    ]
    return " · ".join(parts)


def _home(view: areas.View) -> str:
    mods = areas.MODULES
    return (
        '<section class="pg" id="v-home">'
        + mods["composer"].render(view)
        + f'<div class="listhead"><span class="sum num">{_fmt.esc(_summary(view.snap))}</span></div>'
        + '<div id="list" data-group="need" data-density="comfy">'
        + mods["lead"].render(view)
        + mods["needs_you"].render(view)
        + mods["sessions"].render(view)
        + "</div>"
        + mods["quiet"].render(view)
        + "</section>"
    )


def _evals(view: areas.View) -> str:
    return f'<section class="pg" id="v-evals"><h2 class="vh2">Evals</h2>{areas.MODULES["evals"].render(view)}</section>'


def render(snap: Snapshot, nonce: str = "", *, live: bool = False, hub: areas.Hub | None = None) -> str:
    """The whole page. `live` is True when a server stands behind it (open, answer and start buttons, and refresh);
    `hub` is the box's pickers and what may be answered (`ws.home.hub.build`); `nonce` is the one in that response's
    Content-Security-Policy."""
    view = areas.View(snap, live, hub or areas.Hub())
    body = (
        components.sprite()
        + '<div class="app">'
        + _chrome.sidebar(view)
        + '<div class="scrim" data-act="drawer"></div>'
        + f'<div class="main">{_chrome.topbar(view)}'
        + f'<main class="col" id="main">{_home(view)}{_evals(view)}'
        + f'<section class="pg" id="v-sources"><h2 class="vh2">Sources</h2>{_sources(snap)}</section>'
        + f"{_chrome.coming_pages()}</main></div></div>"
        + _chrome.settings_menu(view)
    )
    return components.page("ws home", body, nonce, CSS, script=SCRIPT_LIVE if live else SCRIPT)
