"""The terminal style every `ws` command prints with: a status glyph and word, headings, key/value rows and plurals.

`Style` is built on `brand.py`: its colour mode (`color_mode`) and its status roles. Status is always a glyph plus a
word (`✓ ok`, `✗ fail`, `! warn`, `– skip`); colour only reinforces it, so the text reads the same without it. Colour
is off for a non-empty `NO_COLOR`, the global `--no-color` flag, `TERM=dumb` and any output that isn't a terminal (a
pipe or a file), and a `FORCE_COLOR` that isn't empty, `0` or `false` turns it on again for a pipe. Padding is
measured on the visible text, so a coloured line and its plain twin line up the same, and stripping the escape codes
from one gives the other exactly.

A stream whose encoding can't hold the glyphs (a Windows console on cp1252, an ASCII pipe) gets ASCII ones (`+ ok`,
`x fail`, `! warn`, `- skip`); the words are the same, so the meaning doesn't change. `Style.write` prints through
`errors="replace"` as a last resort, so an output encoding never crashes a command.

`Style.detect(stream, no_color=...)` reads the environment once and returns the style for that stream. `plural(n, "file")`
gives `1 file` or `83 files` (with a thousands separator), so a count never reads `file(s)`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from typing import IO

from ws.brand import OFF, Ansi, color_mode, visible_len

__all__ = ["ASCII_GLYPHS", "GLYPHS", "KINDS", "Style", "can_encode", "pad", "plural"]

# kind -> glyph. The word beside it is the kind's own name unless the caller gives a more exact one.
GLYPHS = {"ok": "✓", "fail": "✗", "warn": "!", "skip": "–"}
ASCII_GLYPHS = {"ok": "+", "fail": "x", "warn": "!", "skip": "-"}
KINDS = tuple(GLYPHS)


def can_encode(stream: IO[str] | None, text: str) -> bool:
    """Can `stream` write `text`? A stream that names no encoding is taken to be ASCII."""
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def plural(n: int, one: str, many: str | None = None) -> str:
    """`1 file`, `0 files`, `83 files`, `1,234 bytes`; `many` for a word that doesn't just add an s (`entry`, `entries`)."""
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def pad(text: str, width: int) -> str:
    """`text` followed by spaces up to `width` visible columns (colour codes take no room)."""
    return text + " " * max(0, width - visible_len(text))


class Style:
    """Colour helpers for human output. With colour off every method returns plain text."""

    def __init__(self, mode: str | bool = OFF, *, ascii_glyphs: bool = False) -> None:
        self.ansi = Ansi(mode)
        self.color = self.ansi.color
        self.glyphs = ASCII_GLYPHS if ascii_glyphs else GLYPHS

    @classmethod
    def detect(
        cls, stream: IO[str] | None = None, env: Mapping[str, str] | None = None, *, no_color: bool = False
    ) -> Style:
        """The style for `stream` (stdout by default) under `env` (the real environment by default)."""
        stream = sys.stdout if stream is None else stream
        try:
            tty = bool(stream.isatty())
        except (AttributeError, ValueError):  # not a stream with a terminal behind it, or already closed
            tty = False
        return cls(
            color_mode(os.environ if env is None else env, tty, no_color=no_color),
            ascii_glyphs=not can_encode(stream, "".join(GLYPHS.values())),
        )

    def write(self, text: str, stream: IO[str] | None = None) -> None:
        """Print `text` and a newline to `stream` (stdout by default); what the stream's encoding can't hold becomes `?`."""
        stream = sys.stdout if stream is None else stream
        try:
            print(text, file=stream)
        except UnicodeEncodeError:
            enc = getattr(stream, "encoding", None) or "ascii"
            print(text.encode(enc, errors="replace").decode(enc, errors="replace"), file=stream)

    def status(self, kind: str, word: str | None = None, width: int = 0) -> str:
        """`✓ ok`, `✗ fail`, `! warn` or `– skip`, padded to `width` visible columns. `word` is a more exact name for the
        same kind (`unconfigured` is a `warn`)."""
        if kind not in GLYPHS:
            raise ValueError(f"unknown status kind {kind!r}; use one of {', '.join(KINDS)}")
        text = f"{self.glyphs[kind]} {word or kind}"
        painted = {
            "ok": self.ansi.mint,
            "fail": lambda t: self.ansi.error(t, bold=True),
            "warn": self.ansi.amber,
            "skip": self.ansi.dim,
        }[kind](text)
        return pad(painted, width)

    def heading(self, text: str) -> str:
        return self.ansi.bold(text)

    def muted(self, text: str) -> str:
        return self.ansi.dim(text)

    def warn(self, text: str) -> str:
        return self.ansi.amber(text)

    def error(self, text: str) -> str:
        return self.ansi.error(text)

    def kv(self, pairs: Iterable[tuple[str, str]], indent: int = 0) -> list[str]:
        """One `key  value` row per pair with the values lined up; the key is quiet, the value is not."""
        items = [(k, v) for k, v in pairs]
        width = max((len(k) for k, _ in items), default=0)
        return [f"{' ' * indent}{pad(self.muted(k), width)}  {v}" for k, v in items]
