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

__all__ = ["STATES", "mark", "page", "panel", "row", "sparkline", "status"]

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


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


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


def sparkline(values: Iterable[float], *, width: int = 96, height: int = 24, label: str = "trend") -> str:
    """A small line over `values`, oldest first, as inline SVG. Nothing finite gives a dashed baseline; one point gives
    a dot; a flat run gives a level line. NaN, infinity and non-numbers are dropped, never drawn."""
    pts = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    pad = 3
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    shapes = ""
    if not pts:
        mid = _num(height / 2)
        shapes = f'<line class="base" x1="{pad}" y1="{mid}" x2="{width - pad}" y2="{mid}"/>'
        text = f"{label}: no data"
    else:
        lo, hi = min(pts), max(pts)
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


def page(title: str, body: str, nonce: str, extra_css: str = "", *, script: str = "") -> str:
    """A whole page. `body` is HTML made by these functions; `extra_css` is the page's own rules, added after the
    shared ones. `nonce` goes on the stylesheet and on `script` when there is one, and must be the one in this
    response's `csp`."""
    attr = f' nonce="{_e(nonce)}"' if nonce else ""
    sheet = css.stylesheet(css.components_css() + "\n" + extra_css)
    tail = f"\n<script{attr}>{script}</script>" if script else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta name="referrer" content="no-referrer"><title>{_e(title)}</title><style{attr}>{sheet}</style></head>\n'
        f"<body>\n{body}{tail}\n</body></html>"
    )
