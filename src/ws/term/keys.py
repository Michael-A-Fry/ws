"""The keys a phone has no key for: the bytes each key-bar control sends, and the browser-side key rules.

`KEYBAR` is the one table. Each row is a control the page draws, its `data-action`, its label and the bytes it sends,
as the text the browser puts on the wire. A row with `bytes: None` isn't a byte sender: `ctrl` is the sticky modifier
(the next letter becomes `letter & 0x1f`) and `paste` reads the clipboard. The page takes the table as JSON, so the
buttons, the tests and the bytes can't drift apart. `SHIFT_ENTER` is what Shift+Enter and Ctrl+Enter send from a
keyboard too: browsers send a plain carriage return for both, which submits, so a newline in a prompt needs `ESC CR`
(it works without any extended-keys support and through tmux).
"""

from __future__ import annotations

__all__ = ["CTRL_PUNCT", "KEYBAR", "SHIFT_ENTER", "ctrl_byte", "keybar_json"]

ESC = "\x1b"
SHIFT_ENTER = f"{ESC}\r"

# (data-action, label, aria-label, bytes). `bytes` None: the page handles it (see the module docstring).
KEYBAR: tuple[dict[str, str | None], ...] = (
    {"action": "esc", "label": "Esc", "title": "Escape", "bytes": ESC},
    {"action": "tab", "label": "Tab", "title": "Tab", "bytes": "\t"},
    {"action": "shift-tab", "label": "⇤", "title": "Shift+Tab", "bytes": f"{ESC}[Z"},
    {"action": "ctrl", "label": "Ctrl", "title": "Control, applies to the next key", "bytes": None},
    {"action": "up", "label": "↑", "title": "Arrow up", "bytes": f"{ESC}[A"},
    {"action": "down", "label": "↓", "title": "Arrow down", "bytes": f"{ESC}[B"},
    {"action": "right", "label": "→", "title": "Arrow right", "bytes": f"{ESC}[C"},
    {"action": "left", "label": "←", "title": "Arrow left", "bytes": f"{ESC}[D"},
    {"action": "slash", "label": "/", "title": "Slash", "bytes": "/"},
    {"action": "shift-enter", "label": "↵", "title": "New line (Shift+Enter)", "bytes": SHIFT_ENTER},
    {"action": "ctrl-c", "label": "^C", "title": "Interrupt (Ctrl+C)", "bytes": "\x03"},
    {"action": "paste", "label": "Paste", "title": "Paste from the clipboard", "bytes": None},
)

# Control with a non-letter: the byte a terminal makes for it (`ctrl_byte`).
CTRL_PUNCT = {"@": 0x00, " ": 0x00, "[": 0x1B, "\\": 0x1C, "]": 0x1D, "^": 0x1E, "_": 0x1F, "?": 0x7F}


def ctrl_byte(ch: str) -> str:
    """What Ctrl plus this single character sends: a letter becomes `letter & 0x1f`; the punctuation a terminal
    maps (`[`, `\\`, `]`, `^`, `_`, `@`, space, `?`) becomes its control byte; anything else is unchanged.

    The page does the same in JavaScript, taking the punctuation table from `CTRL_PUNCT`; this function is the
    reference, and the browser tests check the page against the same table."""
    if len(ch) != 1:
        return ch
    low = ch.lower()
    if "a" <= low <= "z":
        return chr(ord(low) & 0x1F)
    if ch in CTRL_PUNCT:
        return chr(CTRL_PUNCT[ch])
    return ch


def keybar_json() -> list[dict[str, str | None]]:
    return [dict(row) for row in KEYBAR]
