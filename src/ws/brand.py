"""The ws look in one place: colour tokens by role, the mark, and the rule for when colour is on.

Every surface reads this file: the terminal walk of `ws setup`, the setup page, and any later page. A token is a role
(`accent`, `ok`, `warn`, `error`, `text`, `muted`, `bg`, `panel` and so on), never a colour name, and carries its dark
hex, its light hex, a 256-colour number where a terminal uses it, and a basic-ANSI code for the status roles. The CSS
`:root` blocks are written from the tokens (`css`), so the page holds no hex of its own, and `tokens_json` gives the
same data in a DTCG-shaped dict for other tools. `PAIRS` declares which foreground sits on which background and what
it has to reach (text 4.5, shapes and borders 3, by `contrast`); a test checks every pair, so a value that fails is
caught where it is set.

The terminal rule is `color_mode(env, isatty, no_color=...)`: off for a non-empty `NO_COLOR` or `--no-color`, off for a
pipe or `TERM=dumb`, forced on by a `FORCE_COLOR` that is not empty, `0` or `false` (`NO_COLOR` wins), and otherwise
the full brand palette in 256 colours (mark, rule and status roles). Only when `COLORFGBG` says the background is
light does it drop to "basic": the mark and rule take darker 256-colour numbers that hold 3:1 on white, violet text
holds 4.5:1, and mint, amber, red and grey use the terminal's own colours so its theme can re-colour them. A
terminal that doesn't say (most dark ones over ssh) gets the full palette. `can_redraw` says whether cursor-move and
erase codes are safe (a terminal that is not `TERM=dumb`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ANSI_RE",
    "BASIC",
    "FULL",
    "GRADIENT",
    "GRADIENT_ROLES",
    "KIND_MIN",
    "LOGO",
    "LOGO_SMALL",
    "LOGO_SMALL_WIDTH",
    "LOGO_WIDTH",
    "OFF",
    "PAIRS",
    "RAMP",
    "SMALL_COLOURS",
    "SPINNER",
    "TAGLINE",
    "TOKENS",
    "Ansi",
    "Token",
    "can_redraw",
    "color_mode",
    "colour",
    "contrast",
    "css",
    "dark_background",
    "token",
    "tokens_json",
    "visible_len",
    "xterm_hex",
]


# ---- the tokens ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """One colour role. `xterm` is the 256-colour number a terminal uses (None where only the page uses it). On a
    background known to be light: `basic` is the SGR foreground parameter for a status role, `xterm_light` a darker
    256-colour number for the mark and for violet text; with neither, the role keeps `xterm`."""

    name: str
    dark: str
    light: str
    xterm: int | None
    basic: str | None
    use: str
    xterm_light: int | None = None


TOKENS: tuple[Token, ...] = (
    Token("bg", "#121316", "#f5f5f2", None, None, "the page"),
    Token("panel", "#181a1e", "#ffffff", None, None, "a card or panel"),
    Token("raise", "#1f2126", "#f0f0ec", None, None, "a hovered or inset surface"),
    Token("line", "#272a30", "#e2e2dc", 238, None, "a hairline or the empty part of a track (decoration)"),
    Token("line2", "#6b7180", "#85857f", 245, None, "a control border, a card border, an empty meter cell"),
    Token("text", "#e8e9ec", "#1b1c1f", None, None, "body text"),
    Token("muted", "#92959d", "#62656c", 245, "38;5;243", "quiet text"),
    Token("accent", "#875fff", "#6a3fe0", 99, None, "violet: chosen, current, a filled control", 57),
    Token("accent-text", "#a284ff", "#6a3fe0", 99, None, "violet used as text on a panel"),
    Token("on-accent", "#121316", "#ffffff", None, None, "text or an icon on an accent fill"),
    Token("indigo", "#5f5fff", "#4a4fc8", 63, None, "the mark and the rule, between violet and teal", 62),
    Token("teal", "#00d7af", "#0b7f86", 43, None, "the mark and the rule, between indigo and mint", 30),
    Token("ok", "#00ffaf", "#087a60", 49, "32", "mint: done, added, found", 29),
    Token("warn", "#ffaf00", "#8f5800", 214, "33", "amber: needs a look, a file that changed"),
    Token("error", "#ff5f5f", "#b42318", 203, "31", "red: a refusal"),
)
_BY_NAME = {t.name: t for t in TOKENS}

# Tints of the accent: (role, alpha dark, alpha light). Written as rgba() from the accent so there is no second violet.
DERIVED = (("tint", 0.13, 0.08), ("glow", 0.17, 0.12))

# The mark and the rule, as roles: mostly violet, then indigo, teal and a mint finish.
GRADIENT_ROLES = ("accent", "accent", "accent", "indigo", "teal", "ok")
RAMP_ROLES = ("accent",) * 6 + ("indigo", "indigo", "teal", "teal", "ok", "ok")
SMALL_ROLES = ("accent", "indigo", "ok")


def token(name: str) -> Token:
    return _BY_NAME[name]


def _n(role: str) -> int:
    n = _BY_NAME[role].xterm
    assert n is not None
    return n


GRADIENT = tuple(_n(r) for r in GRADIENT_ROLES)  # 256-colour number per row of the mark
RAMP = tuple(_n(r) for r in RAMP_ROLES)  # the gradient rule, left to right
SMALL_COLOURS = tuple(_n(r) for r in SMALL_ROLES)  # the folded three-row mark

# What a foreground has to reach on its background (WCAG 2.2): text 4.5, everything else a user must see 3.
KIND_MIN = {"text": 4.5, "large": 3.0, "shape": 3.0, "border": 3.0}

# (foreground role, background role, kind) for every pairing the page and the terminal draw. Roles not named as a
# foreground here are surfaces or decoration (bg, panel, raise, line).
PAIRS: tuple[tuple[str, str, str], ...] = (
    *((fg, bg, "text") for fg in ("text", "muted", "ok", "warn") for bg in ("bg", "panel", "raise")),
    ("accent-text", "bg", "text"),
    ("accent-text", "panel", "text"),
    ("error", "bg", "text"),
    ("error", "panel", "text"),
    ("on-accent", "accent", "text"),
    *(("line2", bg, "border") for bg in ("bg", "panel", "raise")),
    *(("accent", bg, "shape") for bg in ("bg", "panel", "raise")),
    *((fg, "bg", "shape") for fg in ("indigo", "teal", "ok")),
    ("error", "panel", "border"),
    # the tint of a chosen card (the accent at low alpha over a panel)
    *((fg, "tint", "text") for fg in ("text", "muted", "ok", "warn")),
    ("line2", "tint", "border"),
)


# ---- contrast -----------------------------------------------------------------------------------------------------


def _channels(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _linear(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    r, g, b = (_linear(c) for c in _channels(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """The WCAG contrast ratio of two `#rrggbb` colours, 1 to 21."""
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def colour(name: str, theme: str) -> str:
    """The `#rrggbb` of a role in a theme. `tint` is the accent at its alpha laid over `panel`, as the page draws it."""
    dark = theme == "dark"
    if name == "tint":
        alpha = next(a_dark if dark else a_light for n, a_dark, a_light in DERIVED if n == name)
        top, under = _channels(colour("accent", theme)), _channels(colour("panel", theme))
        return "#" + "".join(f"{round(t * alpha + u * (1 - alpha)):02x}" for t, u in zip(top, under, strict=True))
    t = _BY_NAME[name]
    return t.dark if dark else t.light


# ---- the page: CSS and a data form --------------------------------------------------------------------------------


def _decls(theme: str) -> str:
    dark = theme == "dark"
    items = [f"--{t.name}:{t.dark if dark else t.light}" for t in TOKENS]
    r, g, b = _channels(_BY_NAME["accent"].dark if dark else _BY_NAME["accent"].light)
    items += [f"--{name}:rgba({r},{g},{b},{a_dark if dark else a_light})" for name, a_dark, a_light in DERIVED]
    items.append(f"color-scheme:{theme}")
    return ";".join(items)


def css() -> str:
    """The `:root` blocks: dark by default, light when the page asks (`data-theme`) or the system does and the page
    hasn't chosen dark. One set of declarations per theme, written once from the tokens."""
    light = _decls("light")
    return (
        f":root{{{_decls('dark')}}}\n"
        f":root[data-theme=light]{{{light}}}\n"
        f"@media(prefers-color-scheme:light){{:root:not([data-theme=dark]){{{light}}}}}"
    )


def tokens_json() -> dict:
    """The tokens as a DTCG-shaped dict (`$type`, `$value`); the other mode and the terminal values sit under
    `$extensions.ws`, because the format has no mode of its own."""
    colour = {}
    for t in TOKENS:
        ext: dict = {"light": t.light, "use": t.use}
        if t.xterm is not None:
            ext["xterm"] = t.xterm
        if t.basic is not None:
            ext["basic"] = t.basic
        colour[t.name] = {"$type": "color", "$value": t.dark, "$extensions": {"ws": ext}}
    return {"color": colour}


# ---- the terminal: when colour is on, and the helpers --------------------------------------------------------------

OFF, BASIC, FULL = "off", "basic", "full"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def dark_background(env: Mapping[str, str]) -> bool | None:
    """True or False when `COLORFGBG` ("fg;bg", set by some terminals) says so; None when it is absent or unreadable."""
    tail = (env.get("COLORFGBG") or "").rsplit(";", 1)[-1]
    if not tail.isdigit():
        return None
    n = int(tail)
    if n in (7, 15):
        return False
    return True if n <= 6 or n == 8 else None


def color_mode(env: Mapping[str, str], isatty: bool, *, no_color: bool = False) -> str:
    """`OFF`, `FULL` (the brand palette in 256 colours) or `BASIC` (only when `COLORFGBG` says the background is light).

    `--no-color` and a non-empty `NO_COLOR` win; then a `FORCE_COLOR` that isn't empty, `0` or `false` (any case) turns
    colour on even for a pipe or `TERM=dumb`; otherwise colour needs a terminal that isn't `TERM=dumb`.
    """
    if no_color or env.get("NO_COLOR"):
        return OFF
    force = env.get("FORCE_COLOR", "").lower() not in ("", "0", "false")
    if not force and (not isatty or env.get("TERM") == "dumb"):
        return OFF
    return BASIC if dark_background(env) is False else FULL


def can_redraw(env: Mapping[str, str], isatty: bool) -> bool:
    """Cursor-move and erase codes are safe: a terminal, and not `TERM=dumb`."""
    return isatty and env.get("TERM") != "dumb"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


_LEVELS = (0, 95, 135, 175, 215, 255)


def xterm_hex(n: int) -> str:
    """The hex colour of a 256-colour number (16 to 231 is the colour cube, 232 and up is the grey ramp)."""
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    r, g, b = _LEVELS[n // 36], _LEVELS[(n // 6) % 6], _LEVELS[n % 6]
    return f"#{r:02x}{g:02x}{b:02x}"


SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
TAGLINE = "Your memory, skills and rules, in any AI."

# "ws" drawn on a pixel grid: each X is two columns wide and one row tall, so the pixels come out square.
_W = ("XX.....XX", "XX.....XX", "XX..X..XX", "XX.XXX.XX", "XXXX.XXXX", ".XX...XX.")
_S = (".XXXXXX", "XX.....", ".XXXXX.", "....XXX", "XX...XX", ".XXXXX.")
LOGO = tuple("".join("██" if c == "X" else "  " for c in w + "." + s).rstrip() for w, s in zip(_W, _S, strict=True))
LOGO_WIDTH = max(len(row) for row in LOGO)
# The same mark folded to three rows for a terminal under 80 columns.
LOGO_SMALL = ("█   █  ▄▀▀▀", "█ █ █  ▀▀▀▄", " ▀▄▀   ▄▄▄▀")
LOGO_SMALL_WIDTH = max(len(row) for row in LOGO_SMALL)


class Ansi:
    """Colour helpers that turn into plain text when colour is off (`OFF`, or `False`).

    `Ansi(True)` is `FULL`. In `BASIC` (a light background) the status roles use the terminal's own colours, and the
    violet, the rule and the mark use darker 256-colour numbers; in `FULL` all of them are the brand numbers.
    """

    def __init__(self, color: bool | str = True) -> None:
        self.mode = (FULL if color else OFF) if isinstance(color, bool) else color
        self.color = self.mode != OFF

    def sgr(self, codes: str, text: str) -> str:
        return f"\x1b[{codes}m{text}\x1b[0m" if self.color and text else text

    def fg(self, n: int, text: str, bold: bool = False) -> str:
        return self.sgr(f"{'1;' if bold else ''}38;5;{n}", text)

    def number(self, name: str) -> int:
        """The 256-colour number of a role in this mode (the mark and the rule use this)."""
        t = _BY_NAME[name]
        n = t.xterm_light if self.mode == BASIC and t.xterm_light is not None else t.xterm
        assert n is not None
        return n

    def role(self, name: str, text: str, bold: bool = False) -> str:
        t = _BY_NAME[name]
        code = t.basic if self.mode == BASIC and t.basic else f"38;5;{self.number(name)}"
        return self.sgr(f"{'1;' if bold else ''}{code}", text)

    def bold(self, text: str) -> str:
        return self.sgr("1", text)

    def dim(self, text: str) -> str:
        return self.role("muted", text)  # a grey, because SGR "dim" is ignored by some terminals

    def violet(self, text: str, bold: bool = False) -> str:
        return self.role("accent", text, bold)

    def mint(self, text: str, bold: bool = False) -> str:
        return self.role("ok", text, bold)

    def amber(self, text: str, bold: bool = False) -> str:
        return self.role("warn", text, bold)

    def error(self, text: str, bold: bool = False) -> str:
        return self.role("error", text, bold)

    def grey(self, text: str) -> str:
        return self.role("line2", text)

    def rule(self, width: int) -> str:
        """A thin line that runs violet to mint across `width` columns."""
        return "".join(self.fg(self.number(RAMP_ROLES[i * len(RAMP_ROLES) // width]), "━") for i in range(width))

    def track(self, done: int, total: int, width: int = 24) -> str:
        """A continuous progress track: violet where done, a dark line for the rest."""
        on = round(done / total * width) if total else width
        return self.violet("━" * on) + self.role("line", "━" * (width - on))

    def logo(self, cols: int | None = None, small: bool = False) -> list[str]:
        """The mark, one colour per row. `cols` shows only the left part (for the sweep-in)."""
        rows, width, roles = (
            (LOGO_SMALL, LOGO_SMALL_WIDTH, SMALL_ROLES) if small else (LOGO, LOGO_WIDTH, GRADIENT_ROLES)
        )
        out = []
        for i, row in enumerate(rows):
            row = row.ljust(width)
            row = row if cols is None else row[:cols].ljust(width)
            out.append(self.fg(self.number(roles[i]), row, bold=True))
        return out
