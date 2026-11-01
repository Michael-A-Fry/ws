"""Path globs for the guards that match a file by where it sits: `**` crosses folders, `*` and `?` stay inside one, and a
pattern with no `/` matches at any depth (`*.css` is `**/*.css`). A path is matched relative to the session's folder when
it sits under it, else by its absolute path with the leading `/` dropped (so `**/` patterns still apply)."""

from __future__ import annotations

import posixpath
import re
from functools import lru_cache


@lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern:
    if "/" not in pattern:
        pattern = "**/" + pattern
    pattern = pattern.removeprefix("./")
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def relative(path: str, cwd: str) -> str:
    """`path` as the guards match it (see the module docstring), with `.` and `..` folded away."""
    full = posixpath.normpath(posixpath.join(cwd or "/", path))
    base = posixpath.normpath(cwd) if cwd else ""
    if base and base != "/" and full.startswith(base + "/"):
        return full[len(base) + 1 :]
    return full.lstrip("/")


def matches(patterns, rel: str) -> bool:
    return any(_regex(p).match(rel) for p in patterns)
