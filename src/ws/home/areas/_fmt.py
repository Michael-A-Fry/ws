"""Small text helpers the areas share: ages, token counts, money, and the buttons and links of a record's `opens`.

Everything here returns text or HTML that already went through `html.escape`; none of it reads anything but its
arguments.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from ws.home.areas._base import View
from ws.home.records import HIDDEN, NOT_MEASURED
from ws.ui import components

OPAQUE_LINE = "Open the session to see this"


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def age(ts: str | None, now: str | None) -> str:
    """How long before `now` a time was, in the largest whole unit: `just now`, `5 min ago`, `3 h ago`, `2 d ago`."""
    then, here = _when(ts), _when(now)
    if then is None or here is None:
        return ""
    secs = int((here - then).total_seconds())
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size} {unit} ago"
    return ""


def count(n: float) -> str:
    """A count as a short figure: 950, 12.3k, 4.1M, 2.5G."""
    for size, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= size:
            v = n / size
            return f"{v:.1f}".rstrip("0").rstrip(".") + suffix if v < 100 else f"{v:.0f}{suffix}"
    return f"{n:.0f}"


def tokens(rec) -> str:
    """The session's tokens, all four kinds added, or why there is no figure."""
    if rec.opaque:
        return HIDDEN
    t = rec.tokens
    if not t:
        return NOT_MEASURED
    return count(sum(v for v in t.values() if isinstance(v, (int, float)))) + " tokens"


def cost(rec) -> str:
    """The session's cost in US dollars, or `not measured` (`hidden` when the session is opaque)."""
    if rec.opaque:
        return HIDDEN
    if rec.cost_usd is None:
        return NOT_MEASURED
    return f"US${rec.cost_usd:,.2f}"


def usd(value: float | None) -> str:
    """Money per solved task: four decimals under ten cents, else two."""
    if value is None:
        return NOT_MEASURED
    return f"US${value:.4f}" if value < 0.1 else f"US${value:,.2f}"


def seconds(value: float | None) -> str:
    if value is None:
        return NOT_MEASURED
    return f"{value:.1f} s" if value < 60 else f"{value / 60:.1f} min"


def percent(rate: float | None) -> str:
    if rate is None:
        return NOT_MEASURED
    pct = rate * 100
    return f"{pct:.0f}%" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}%"


def opens(view: View, rec, kind: str) -> str:
    """The ways to open one session or ask, as links and buttons (or lines to paste when no server stands behind the
    page). Nothing is drawn for a record with none."""
    parts: list[str] = []
    for o in rec.opens:
        if o.get("href"):
            parts.append(components.link(o["label"], o["href"]))
        elif o.get("post") and view.live:
            parts.append(components.action(o["label"], opener=o["opener"], kind=kind, id=rec.id))
        elif o.get("text"):
            parts.append(f'<code class="cmd" title="{esc(o["label"])}">{esc(o["text"])}</code>')
    return f'<span class="acts">{"".join(parts)}</span>' if parts else ""
