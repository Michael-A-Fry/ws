"""The terminal style every `ws` command prints with: a status glyph and word, headings, key/value rows and plurals.

`Style` is built on `brand.py`: its colour mode (`color_mode`) and its status roles. Status is always a glyph plus a
word (`✓ ok`, `✗ fail`, `! warn`, `– skip`); colour only reinforces it, so the text reads the same without it. Colour
is off for a non-empty `NO_COLOR`, the global `--no-color` flag, `TERM=dumb` and any output that isn't a terminal (a
pipe or a file), and a `FORCE_COLOR` that isn't empty, `0` or `false` turns it on again for a pipe. Padding is
measured on the visible text, so a coloured line and its plain twin line up the same, and stripping the escape codes
from one gives the other exactly.

`Style.detect(stream, no_color=...)` reads the environment once and returns the style for that stream. `plural(n, "file")`
gives `1 file` or `83 files` (with a thousands separator), so a count never reads `file(s)`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from typing import IO

from ws.brand import OFF, Ansi, color_mode, visible_len

__all__ = ["GLYPHS", "KINDS", "Style", "pad", "plural"]

# kind -> glyph. The word beside it is the kind's own name unless the caller gives a more exact one.
GLYPHS = {"ok": "✓", "fail": "✗", "warn": "!", "skip": "–"}
KINDS = tuple(GLYPHS)


def plural(n: int, one: str, many: str | None = None) -> str:
    """`1 file`, `0 files`, `83 files`, `1,234 bytes`; `many` for a word that doesn't just add an s (`entry`, `entries`)."""
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def pad(text: str, width: int) -> str:
    """`text` followed by spaces up to `width` visible columns (colour codes take no room)."""
    return text + " " * max(0, width - visible_len(text))


class Style:
    """Colour helpers for human output. With colour off every method returns plain text."""

    def __init__(self, mode: str | bool = OFF) -> None:
        self.ansi = Ansi(mode)
        self.color = self.ansi.color

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
        return cls(color_mode(os.environ if env is None else env, tty, no_color=no_color))

    def status(self, kind: str, word: str | None = None, width: int = 0) -> str:
        """`✓ ok`, `✗ fail`, `! warn` or `– skip`, padded to `width` visible columns. `word` is a more exact name for the
        same kind (`unconfigured` is a `warn`)."""
        if kind not in GLYPHS:
            raise ValueError(f"unknown status kind {kind!r}; use one of {', '.join(KINDS)}")
        text = f"{GLYPHS[kind]} {word or kind}"
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
