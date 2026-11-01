"""The shared stylesheet: one type, space and radius scale and the base rules, built from the `ws.brand` tokens.

The rules name colours as `var(--role)` and hold none of their own: `ws.brand.css()` writes the `:root` blocks for the
dark and light themes, so every page that reads this file follows the tokens. `TYPE`, `SPACE` and `RADIUS` are the
scale the shared rules draw from; a page adds its own rules after them through `stylesheet(extra)`. All of it is
returned as strings, because package data ships as Python (`ws secret wall` copies only `.py` files).
"""

from __future__ import annotations

from ws import brand

__all__ = [
    "BASE",
    "FONT_MONO",
    "FONT_SANS",
    "PANEL",
    "RADIUS",
    "SPACE",
    "TYPE",
    "components_css",
    "mark_css",
    "stylesheet",
]

FONT_SANS = 'ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif'
FONT_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# The scale. A rule below uses these names; a page that wants a size picks one of them rather than a new number.
TYPE = {"xs": "11.5px", "sm": "13px", "md": "14px", "base": "15px", "lg": "18px", "xl": "30px"}
SPACE = {"xs": "6px", "sm": "10px", "md": "14px", "lg": "18px", "xl": "28px"}
RADIUS = {"xs": "5px", "sm": "8px", "md": "12px", "lg": "14px", "pill": "999px"}

# Reset, page, fonts, focus ring and the page's width. Every page starts with these.
BASE = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(900px 280px at 6% -60px,var(--glow),transparent 70%) no-repeat,
var(--bg);color:var(--text);font:{TYPE["base"]}/1.5 {FONT_SANS};
min-height:100vh}}
code,.mono,input[type=text]{{font-family:{FONT_MONO}}}
button{{font:inherit;color:inherit;background:none;border:0;cursor:pointer}}
svg{{display:block;flex:none}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.wrap{{max-width:1200px;margin:0 auto;padding:0 {SPACE["xl"]}}}
"""

# A titled panel: a heading line with a quiet aside, then a body. Setup and the home page both draw it.
PANEL = f""".panel{{background:var(--panel);border:1px solid var(--line);border-radius:{RADIUS["lg"]};margin:0 0 24px;overflow:hidden}}
.panel h2{{display:flex;justify-content:space-between;align-items:center;margin:0;padding:12px {SPACE["lg"]};font-size:{TYPE["md"]};
font-weight:600;border-bottom:1px solid var(--line)}}
.panel h2 span{{color:var(--muted);font-weight:400;font-size:{TYPE["sm"]}}}
.panel h2 span.mint{{color:var(--ok);font-size:{TYPE["base"]};font-weight:650}}
.panel ul{{list-style:none;margin:0;padding:4px 0 6px}}
"""

# What the components draw that setup does not: a key/value row, a status glyph and word, a sparkline.
_COMPONENTS = f"""
.row{{display:grid;grid-template-columns:minmax(90px,max-content) 1fr;gap:{SPACE["xs"]} {SPACE["md"]};padding:6px {SPACE["lg"]}}}
.row .k{{color:var(--muted)}}.row .v{{min-width:0;word-break:break-word}}
.st{{display:inline-flex;align-items:center;gap:6px;font-size:{TYPE["sm"]};white-space:nowrap}}
.st .g{{font-size:{TYPE["md"]};line-height:1}}.st .w{{color:var(--text)}}
.st-ok .g{{color:var(--ok)}}.st-warn .g{{color:var(--warn)}}.st-error .g{{color:var(--error)}}
.st-info .g{{color:var(--accent)}}.st-muted .g{{color:var(--muted)}}
.spark{{width:96px;height:24px;color:var(--accent);overflow:visible}}
.spark .base{{stroke:var(--line2);stroke-width:1;stroke-dasharray:2 3}}
.spark .ln{{fill:none;stroke:currentColor;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}}
.spark .pt{{fill:currentColor}}
"""


def mark_css() -> str:
    """One class per row of the mark, each naming the role it takes its colour from (a darker ramp in light mode)."""
    return "\n".join(f".logo .r{i}{{color:var(--{role})}}" for i, role in enumerate(brand.GRADIENT_ROLES))


def components_css() -> str:
    """The rules for `row`, `status` and `sparkline`, which a page adds after the base."""
    return _COMPONENTS.strip("\n")


def stylesheet(extra: str = "") -> str:
    """The whole stylesheet: the brand `:root` blocks, the base, the page's own `extra` rules, then the mark's rows."""
    parts = [brand.css(), BASE.strip("\n"), extra.strip("\n"), mark_css()]
    return "\n".join(p for p in parts if p)
