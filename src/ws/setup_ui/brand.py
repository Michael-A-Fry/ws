"""The ws identity, shared by the inline setup and the web page (see the Visual kit in `ws.setup_ui`).

One brand pair: violet (chosen, current, the name) and mint (done, added, found). Colours are 256-colour numbers so
the terminal and the page agree (`xterm_hex` turns one into the page's hex).
"""

from __future__ import annotations

import re

VIOLET = 99  # #875fff
MINT = 49  # #00ffaf
AMBER = 214  # #ffaf00, needs a look: a warning or a refusal
GREY = 245  # card borders
DIM = 245  # quiet text; a grey number, because SGR "dim" is ignored by some terminals
TRACK = 238  # the empty part of a progress track
GRADIENT = (99, 99, 99, 63, 43, 49)  # the mark, one colour per row: mostly violet, then indigo, teal and a mint finish
RAMP = (99,) * 6 + (63, 63, 43, 43, 49, 49)  # the gradient rule, left to right
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
TAGLINE = "Your memory, skills and rules, in any AI."

# "ws" drawn on a pixel grid: each X is two columns wide and one row tall, so the pixels come out square.
_W = ("XX.....XX", "XX.....XX", "XX..X..XX", "XX.XXX.XX", "XXXX.XXXX", ".XX...XX.")
_S = (".XXXXXX", "XX.....", ".XXXXX.", "....XXX", "XX...XX", ".XXXXX.")
LOGO = tuple("".join("██" if c == "X" else "  " for c in w + "." + s).rstrip() for w, s in zip(_W, _S))
LOGO_WIDTH = max(len(row) for row in LOGO)
# The same mark folded to three rows for a terminal under 80 columns.
LOGO_SMALL = ("█   █  ▄▀▀▀", "█ █ █  ▀▀▀▄", " ▀▄▀   ▄▄▄▀")
LOGO_SMALL_WIDTH = max(len(row) for row in LOGO_SMALL)
SMALL_COLOURS = (99, 63, 49)

_LEVELS = (0, 95, 135, 175, 215, 255)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def xterm_hex(n: int) -> str:
    """The hex colour of a 256-colour number (16 to 231 is the colour cube, 232 and up is the grey ramp)."""
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    r, g, b = _LEVELS[n // 36], _LEVELS[(n // 6) % 6], _LEVELS[n % 6]
    return f"#{r:02x}{g:02x}{b:02x}"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


class Ansi:
    """Colour helpers that turn into plain text when colour is off (NO_COLOR, a pipe, a dumb terminal)."""

    def __init__(self, color: bool = True) -> None:
        self.color = color

    def sgr(self, codes: str, text: str) -> str:
        return f"\x1b[{codes}m{text}\x1b[0m" if self.color and text else text

    def fg(self, n: int, text: str, bold: bool = False) -> str:
        return self.sgr(f"{'1;' if bold else ''}38;5;{n}", text)

    def bold(self, text: str) -> str:
        return self.sgr("1", text)

    def dim(self, text: str) -> str:
        return self.fg(DIM, text)

    def violet(self, text: str, bold: bool = False) -> str:
        return self.fg(VIOLET, text, bold)

    def mint(self, text: str, bold: bool = False) -> str:
        return self.fg(MINT, text, bold)

    def grey(self, text: str) -> str:
        return self.fg(GREY, text)

    def rule(self, width: int) -> str:
        """A thin line that runs violet to mint across `width` columns."""
        return "".join(self.fg(RAMP[i * len(RAMP) // width], "━") for i in range(width))

    def track(self, done: int, total: int, width: int = 24) -> str:
        """A continuous progress track: violet where done, a dark line for the rest."""
        on = round(done / total * width) if total else width
        return self.violet("━" * on) + self.fg(TRACK, "━" * (width - on))

    def logo(self, cols: int | None = None, small: bool = False) -> list[str]:
        """The mark, one colour per row. `cols` shows only the left part (for the sweep-in)."""
        rows, width, colours = (LOGO_SMALL, LOGO_SMALL_WIDTH, SMALL_COLOURS) if small else (LOGO, LOGO_WIDTH, GRADIENT)
        out = []
        for i, row in enumerate(rows):
            row = row.ljust(width)
            row = row if cols is None else row[:cols].ljust(width)
            out.append(self.fg(colours[i], row, bold=True))
        return out
