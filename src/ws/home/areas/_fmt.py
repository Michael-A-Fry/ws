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


def age_short(ts: str | None, now: str | None) -> str:
    """The age in a few characters for the right of a row: `now`, `12m`, `3h`, `2d`; empty when there is no time."""
    then, here = _when(ts), _when(now)
    if then is None or here is None:
        return ""
    secs = int((here - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return "now"


def within(ts: str | None, now: str | None, seconds: int) -> bool:
    """True when `ts` is no more than `seconds` before `now` (a time in the future counts)."""
    then, here = _when(ts), _when(now)
    return then is not None and here is not None and (here - then).total_seconds() <= seconds


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


LINK_LABELS = {"claude_app": "Claude app", "browser": "Browser"}
OPEN_LABEL = "Open →"
PHONE_WHY = "terminal only"


def _button(view: View, o: dict, kind: str, rec, *, copy_too: bool) -> str:
    """The Open button for one call: it runs the call when a server stands behind the page, else it copies the line.
    The line itself is the button's hover text, never its label; `copy_too` adds a small copy control beside a button
    that runs."""
    text = o.get("text", "")
    if o.get("post") and view.live:
        run = components.action(
            OPEN_LABEL, opener=o["opener"], kind=kind, id=rec.id, action="open.run", expect="post:/api/open"
        )
        more = (
            components.copy_button(
                text, "Copy", small=True, title="Copy the command", action="open.copy", expect="copy"
            )
            if copy_too and text
            else ""
        )
        return run + more
    if text:
        return components.copy_button(text, OPEN_LABEL, title=f"Copies: {text}", action="open.copy", expect="copy")
    return ""


def open_cell(view: View, rec, kind: str) -> str:
    """How to open one session or ask: a labelled `Open →` button, and a `Claude app` link when the record carries one.

    At desktop width the button is the terminal call (or your own `command` when there is no terminal one). At phone
    width it is your `command` opener when you set one, else a greyed-out button that says `terminal only`. A record
    with nothing to open it with draws nothing."""
    by_name = {o["opener"]: o for o in rec.opens if not o.get("href")}
    links = "".join(
        components.link(
            LINK_LABELS.get(o["opener"], o["label"]), o["href"], action="open.link", expect="navigate:external"
        )
        for o in rec.opens
        if o.get("href")
    )
    desk_call = by_name.get("terminal") or by_name.get("command")
    phone_call = by_name.get("command")
    if desk_call is None and not links:
        return ""
    desk = _button(view, desk_call, kind, rec, copy_too=True) if desk_call else ""
    if phone_call is not None:
        phone = _button(view, phone_call, kind, rec, copy_too=False)
    elif desk_call is not None:
        phone = (
            components.action(
                OPEN_LABEL, disabled=True, title=PHONE_WHY, action="open.run", expect="none:terminal only"
            )
            + f'<span class="why">{PHONE_WHY}</span>'
        )
    else:
        phone = ""
    return (
        '<span class="acts">'
        + (f'<span class="o-desk">{desk}</span>' if desk else "")
        + (f'<span class="o-phone">{phone}</span>' if phone else "")
        + links
        + "</span>"
    )
