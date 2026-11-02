"""Small pure functions that return HTML fragments: a panel, a row, a status, the ws mark, a sparkline and a page.

Every piece of text goes through `html.escape` here, so a caller hands over plain text and never builds markup. The one
exception is a body: `panel` and `page` take HTML that was itself made by these functions. A status is always a glyph
and a word, never a colour alone, and a sparkline is an inline SVG with attributes only (no `style=`), so the page's
Content-Security-Policy needs nothing but the nonce on its one stylesheet.
"""

from __future__ import annotations

import html
import math
from collections.abc import Iterable

from ws import brand
from ws.ui import css

__all__ = [
    "SAFE_SCHEMES",
    "STATES",
    "action",
    "chip",
    "copy_button",
    "empty",
    "icon",
    "link",
    "mark",
    "page",
    "panel",
    "row",
    "sparkline",
    "sprite",
    "stat",
    "status",
]

# A link is drawn only for these schemes; anything else (javascript:, data:, a path) is shown as plain text.
SAFE_SCHEMES = ("http://", "https://", "claude://")

# state -> (css class, glyph, default word). A glyph differs per state so the shape tells it apart without colour.
STATES: dict[str, tuple[str, str, str]] = {
    "ok": ("st-ok", "✓", "ok"),
    "warn": ("st-warn", "▲", "needs a look"),
    "error": ("st-error", "✕", "failed"),
    "working": ("st-info", "●", "working"),
    "waiting": ("st-warn", "◆", "waiting"),
    "idle": ("st-muted", "○", "idle"),
    "ended": ("st-muted", "◌", "ended"),
    "unknown": ("st-muted", "?", "unknown"),
}


# Line icons on a 24 by 24 grid, stroked in the text colour: name -> the shapes inside its <symbol>.
ICONS: dict[str, str] = {
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "new": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "hist": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 8v4l3 2"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>'
    '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "blocks": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 17.5h7M17.5 14v7"/>',
    "plug": '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
    "gauge": '<path d="M4 18a9 9 0 1 1 16 0"/><path d="M12 14l4-5"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "chev": '<path d="M6 9l6 6 6-6"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h10"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "mon": '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M8 20h8M12 16v4"/>',
    "term": '<path d="M4 17l6-5-6-5M12 19h8"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "layers": '<path d="M12 3 2 8l10 5 10-5z"/><path d="M2 16l10 5 10-5"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="2"/>'
    '<path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "folder": '<path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
    "sliders": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
    '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "gear": '<circle cx="12" cy="12" r="3"/>'
    '<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
}


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def sprite() -> str:
    """The icon shapes as one hidden SVG of `<symbol>`s, written once per page; `icon` points into it."""
    body = "".join(f'<symbol id="i-{n}" viewBox="0 0 24 24">{shapes}</symbol>' for n, shapes in ICONS.items())
    return f'<svg width="0" height="0" class="sprite" aria-hidden="true" focusable="false">{body}</svg>'


def icon(name: str, extra: str = "") -> str:
    """A line icon from the sprite, 16 px unless the page's rules say otherwise. `extra` adds a class (a plain word). A
    name not in `ICONS` draws nothing."""
    if name not in ICONS:
        return ""
    more = f" {_e(extra)}" if extra else ""
    return f'<svg class="i{more}" aria-hidden="true" focusable="false"><use href="#i-{name}"/></svg>'


def panel(title: str, body: str, *, aside: str = "") -> str:
    """A titled panel. `title` and `aside` are text; `body` is HTML made by these functions."""
    tail = f"<span>{_e(aside)}</span>" if aside else ""
    return f'<section class="panel"><h2>{_e(title)}{tail}</h2>{body}</section>'


def row(label: str, value: object) -> str:
    """One key/value row. Both are text."""
    return f'<div class="row"><span class="k">{_e(label)}</span><span class="v">{_e(value)}</span></div>'


def status(state: str, word: str | None = None) -> str:
    """A glyph and a word. An unknown `state` reads as "unknown", never as a made-up colour. `word` is text."""
    cls, glyph, default = STATES.get(state, STATES["unknown"])
    shown = default if word is None else word
    return f'<span class="st {cls}"><span class="g" aria-hidden="true">{glyph}</span><span class="w">{_e(shown)}</span></span>'


def mark() -> str:
    """The pixel mark as text: one class per row, coloured by the stylesheet."""
    rows = "\n".join(f'<i class="r{i}">{row_}</i>' for i, row_ in enumerate(brand.LOGO))
    return f'<pre class="logo" role="img" aria-label="ws">{rows}</pre>'


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def sparkline(
    values: Iterable[float],
    *,
    width: int = 96,
    height: int = 24,
    label: str = "trend",
    lo: float | None = None,
    hi: float | None = None,
) -> str:
    """A small line over `values`, oldest first, as inline SVG. Nothing finite gives a dashed baseline; one point gives
    a dot; a flat run gives a level line. NaN, infinity and non-numbers are dropped, never drawn. `lo` and `hi` fix the
    scale (a rate from 0 to 1, say) so a small wobble isn't drawn as a cliff; without them the line fills the box."""
    width, height = int(width), int(height)
    pts = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    pad = 3
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    shapes = ""
    if not pts:
        mid = _num(height / 2)
        shapes = f'<line class="base" x1="{pad}" y1="{mid}" x2="{width - pad}" y2="{mid}"/>'
        text = f"{label}: no data"
    else:
        lo = min(pts) if lo is None else min(lo, *pts)
        hi = max(pts) if hi is None else max(hi, *pts)
        span = hi - lo

        def y(v: float) -> float:
            return height / 2 if span == 0 else pad + inner_h * (1 - (v - lo) / span)

        if len(pts) == 1:
            shapes = f'<circle class="pt" cx="{_num(width / 2)}" cy="{_num(y(pts[0]))}" r="2.5"/>'
        else:
            step = inner_w / (len(pts) - 1)
            path = " ".join(f"{_num(pad + i * step)},{_num(y(v))}" for i, v in enumerate(pts))
            end_x, end_y = _num(pad + inner_w), _num(y(pts[-1]))
            shapes = f'<polyline class="ln" points="{path}"/><circle class="pt" cx="{end_x}" cy="{end_y}" r="2.5"/>'
        text = f"{label}: {len(pts)} point{'s' if len(pts) != 1 else ''}, from {_num(pts[0])} to {_num(pts[-1])}"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'aria-label="{_e(text)}">{shapes}</svg>'
    )


def empty(text: str) -> str:
    """The quiet line a panel shows when it has nothing to show. `text` says why, and what to do."""
    return f'<p class="empty">{_e(text)}</p>'


def chip(text: str) -> str:
    """A small outlined label, for a kind or a source. Text only."""
    return f'<span class="chip">{_e(text)}</span>'


def stat(label: str, value: object, note: str = "") -> str:
    """One figure with its label and, under it, a quiet note (why it has no value, or what it is measured over)."""
    tail = f'<span class="n">{_e(note)}</span>' if note else ""
    return f'<div class="stat"><span class="k">{_e(label)}</span><span class="v">{_e(value)}</span>{tail}</div>'


def _data(data: dict) -> str:
    return "".join(f' data-{_e(k.replace("_", "-"))}="{_e(v)}"' for k, v in data.items())


def link(label: str, href: str, **data: str) -> str:
    """A plain `<a href>` that opens in a new tab (it works on a phone). A `href` that isn't http, https or claude is
    drawn as text, never as a link. `data` becomes `data-*` attributes, as in `action`."""
    if not href.lower().startswith(SAFE_SCHEMES):
        return f'<span class="act off">{_e(label)}</span>'
    return f'<a class="act" href="{_e(href)}" target="_blank" rel="noopener noreferrer"{_data(data)}>{_e(label)}</a>'


def action(label: str, *, disabled: bool = False, title: str = "", **data: str) -> str:
    """A button the page's script turns into a POST. `data` becomes `data-*` attributes (`_` in a name is `-`).
    `disabled` draws it greyed out; `title` is the hover text (say why when it is disabled)."""
    attrs = _data(data)
    attrs += f' title="{_e(title)}"' if title else ""
    attrs += " disabled" if disabled else ""
    return f'<button type="button" class="act"{attrs}>{_e(label)}</button>'


def copy_button(text: str, label: str, *, small: bool = False, title: str = "", **data: str) -> str:
    """A button the page's script turns into "copy `text`". `label` is what shows; `text` only rides in `data-copy`,
    so a command can sit behind a button and never be its label."""
    cls = "act sm" if small else "act"
    hover = f' title="{_e(title)}"' if title else ""
    return f'<button type="button" class="{cls}" data-copy="{_e(text)}"{hover}{_data(data)}>{_e(label)}</button>'


def page(title: str, body: str, nonce: str, extra_css: str = "", *, script: str = "") -> str:
    """A whole page. `body` is HTML made by these functions; `extra_css` is the page's own rules, added after the
    shared ones. `nonce` goes on the stylesheet and on `script` when there is one, and must be the one in this
    response's `csp`."""
    attr = f' nonce="{_e(nonce)}"' if nonce else ""
    sheet = css.stylesheet(css.components_css() + "\n" + extra_css)
    tail = f"\n<script{attr}>{script}</script>" if script else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f'<meta name="referrer" content="no-referrer"><title>{_e(title)}</title><style{attr}>{sheet}</style></head>\n'
        f"<body>\n{body}{tail}\n</body></html>"
    )
