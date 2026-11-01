"""Argument templates: the program lines in the profile's `[home.start]` and `[home.answer]`, checked and filled.

A template is a list of words, the first the program: `["ws", "dispatch", "{workspace}", "{request}"]`. Each `{name}` in
a word is replaced by a value; nothing goes through a shell, so a value is one argument and can't add another. A value
that would start a word with a dash is refused (the program would read it as a flag), and so is one with a NUL.
`check` runs when the profile is read (every name must be one the table allows); `fill` runs when a call is made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

__all__ = ["check", "fill", "names"]

_NAME = re.compile(r"\{(\w+)\}")


def names(words: Sequence[str]) -> set[str]:
    """Every `{name}` the words use."""
    return {n for w in words for n in _NAME.findall(w)}


def check(words: Sequence[str], allowed: Sequence[str], required: str) -> str:
    """Why `words` can't be a template for a table that allows `allowed` names and needs `required`; "" when it can."""
    if not words:
        return "must name a program"
    if _NAME.search(words[0]):
        return "the program (the first word) can't hold a {name}"
    used = names(words)
    unknown = sorted(used - set(allowed))
    if unknown:
        return f"unknown name {{{unknown[0]}}}; the names are: " + ", ".join("{" + a + "}" for a in allowed)
    if required not in used:
        return f"must use {{{required}}}"
    return ""


def fill(words: Sequence[str], values: Mapping[str, str]) -> list[str]:
    """The argument list: each `{name}` replaced by its value. Raises ValueError, in plain words, for a value that is
    missing, holds a NUL, or would start a word with a dash."""
    out: list[str] = []
    for word in words:
        built = word
        for name in _NAME.findall(word):
            if name not in values:
                raise ValueError(f"no value for {name}")
            value = values[name]
            if "\0" in value:
                raise ValueError(f"{name} can't hold a NUL character")
            if value.startswith("-") and word.startswith("{" + name + "}"):
                raise ValueError(f"{name} can't start with a dash")
            built = built.replace("{" + name + "}", value)
        out.append(built)
    return out
