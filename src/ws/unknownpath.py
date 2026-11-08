"""A command word with a part the reader cannot resolve, tested against the paths ws protects.

The reader (ws.policy) expands what a command line sets itself (`HOME`, `WS_HOME`, `X=/y; cat $X`). A variable it was
not told about (`$REPORT`, `${A}`), a command substitution (`$(cat dir.txt)`, backticks) and an arithmetic expansion
have a value only the shell will know, and a session's own settings can put anything in the environment. So a word
holding one is read as an allow-list reads it: the unresolved part stands for ANY string, slashes and leading folders
included, and the word is refused when some value of that part would make it a path a rule protects. This module is the
pure part of that: it splits a word into known characters and unresolved parts (`split`), drops what an unresolved part
could climb out of (`unconfined`: it may hold `..`, so a folder written before it confines nothing), and decides whether
some string fits both the word and a path glob (`reaches`). The reader chooses the words and the rules and words the
refusal.
Python 3.11+, standard library only.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Collection
from functools import lru_cache

MAX_WORD = (
    2000  # characters of a word judged here; a longer one is refused by the caller (it cannot be modelled in time)
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPECIAL = frozenset("@*#?$!-0123456789")
_COUNTERS = frozenset(  # parameters whose value is a number: no slash, no dot, so no way up out of a folder
    ["RANDOM", "SECONDS", "LINENO", "BASHPID", "PPID", "UID", "EUID", "SHLVL", "BASH_SUBSHELL", "EPOCHSECONDS"]
)
_Item = str | None  # one character the word spells out, or None for a part whose value is not known
_Token = tuple[str, str]  # (kind, text): kind is c (a character), ? (one non-/), * (non-/ run), ** (any run) or cls


def _close(word: str, i: int, open_: str, close: str) -> int:
    """The index just past the `close` that matches the `open_` at word[i], or len(word) when there is none (the rest of
    the word is then part of it, the safe reading). Quotes are not followed: an odd one only makes the part longer."""
    depth, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "\\":
            i += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def split(word: str, known: Collection[str] = (), producer: Callable[[str], str | None] | None = None) -> list[_Item]:
    """`word` as one item per character, with None for each part the reader could not resolve: `$NAME`, `${...}`,
    `$(...)`, `$((...))`, backticks and the special parameters (`$1`, `$@`, `$$`). A name in `known` is left as the text it
    is (the reader substitutes known names, so one still in a word was quoted). Neighbouring unresolved parts are one.
    `producer` is asked about the command inside a `$(...)` or a pair of backticks and returns the text that command
    prints when that is known (see `ws.policy._produced`), else None, which leaves the part unresolved."""
    out: list[_Item] = []
    i, n = 0, len(word)

    def unknown() -> None:
        if not out or out[-1] is not None:
            out.append(None)

    while i < n:
        c = word[i]
        nxt = word[i + 1 : i + 2]
        if c == "`":
            end = _close_tick(word, i)
            made = producer(word[i + 1 : end - 1]) if producer and end - 1 > i and word[end - 1] == "`" else None
            if made is None:
                unknown()
            else:
                out += list(made)
            i = end
        elif c == "$" and nxt == "(":
            end = _close(word, i + 1, "(", ")")
            inner = word[i + 2 : end - 1]
            closed = end - 1 > i + 1 and word[end - 1] == ")" and not inner.startswith("(")
            made = producer(inner) if producer and closed else None
            if inner.startswith("(") and inner.endswith(")") and word[end - 1 : end] == ")":
                made = "0"  # `$((i+1))`: arithmetic gives a number (what it runs is read apart, see ws.policy)
            if made is None:
                unknown()
            else:
                out += list(made)
            i = end
        elif c == "$" and nxt == "{":
            end = _close(word, i + 1, "{", "}")
            head = _NAME.match(word[i + 2 : end - 1])
            if head and head.group() in known and head.end() == len(word[i + 2 : end - 1]):
                out += list(word[i:end])
            else:
                unknown()
            i = end
        elif c == "$" and (m := _NAME.match(word, i + 1)):
            if m.group() in known:
                out += list(word[i : m.end()])
            elif m.group() in _COUNTERS:
                out.append("0")
            else:
                unknown()
            i = m.end()
        elif c == "$" and nxt and nxt in "$?#!":  # the shell's own numbers: a process id, a status, a count
            out.append("0")
            i += 2
        elif c == "$" and nxt in _SPECIAL:
            unknown()
            i += 2
        else:
            out.append(c)
            i += 1
    return out


def _close_tick(word: str, i: int) -> int:
    j = i + 1
    while j < len(word):
        if word[j] == "\\":
            j += 2
            continue
        if word[j] == "`":
            return j + 1
        j += 1
    return len(word)


def has_unknown(items: list[_Item]) -> bool:
    return None in items


def is_bare(items: list[_Item]) -> bool:
    """The whole word is one unresolved part (`$X`, `"$X"`, `$(cmd)`, `${X:-y}`)."""
    return items == [None]


def anchor(items: list[_Item], cwd: str, cwd_known: bool = True) -> list[_Item]:
    """The word as an absolute pattern. A relative word starts at the folder the command runs in (any folder, when a `cd`
    went to a place the reader could not resolve). The leading run of known characters is tidied (`..`, `//`, `/./`)."""
    if not items:
        return items
    first = items[0]
    if first is None or first == "/":
        pass
    elif not cwd_known:
        items = [None, "/", *items]
    else:
        items = [*cwd.rstrip("/"), "/", *items]
    k = next((x for x, it in enumerate(items) if it is None), len(items))
    if k:  # tidy the leading run of known characters
        run = re.sub(r"/{2,}", "/", "".join(items[:k]))  # type: ignore[arg-type]
        tidy = posixpath.normpath(run) if run.startswith("/") else run
        if run.endswith("/") and not tidy.endswith("/"):
            tidy += "/"
        items = [*tidy, *items[k:]]
    return items


def unconfined(items: list[_Item]) -> list[_Item]:
    """The word as a path where the unresolved parts may hold `..`. A part that is not known can climb out of any folder
    written before it (`/tmp/$X` with X=`../home/pat/.ssh`), so known text confines a path only AFTER the first unresolved
    part: everything before it is dropped, and so is everything up to the last `..` of each known run after it (`$X/../a`
    is a path that ends in `/a` and starts anywhere). Runs of `/` and `/./` are tidied. A word with no unresolved part is
    returned as it is."""
    if None not in items:
        return items
    out: list[_Item] = [None]
    run: list[str] = []

    def flush() -> None:
        text = re.sub(r"/{2,}", "/", "".join(run))
        text = re.sub(r"/(?:\./)+", "/", text)
        run.clear()
        cut = text.rfind("..")
        if cut >= 0:
            text = text[cut + 2 :]
            if out[-1] is not None:
                out.append(None)
        out.extend(text)

    for it in items[items.index(None) :]:
        if it is None:
            flush()
            if out[-1] is not None:
                out.append(None)
        else:
            run.append(it)
    flush()
    return out


@lru_cache(maxsize=512)
def tokens(glob: str) -> tuple[tuple[_Token, ...], ...]:
    """A path glob as token lists, one for each way its `**/` can be taken (a `**/` is a run of folders or nothing). More
    than three of them are read as `**` alone, which takes in everything they would."""
    parts = re.split(r"(\*\*/)", glob)
    if sum(p == "**/" for p in parts) > 3:
        glob = glob.replace("**/", "**")
        parts = [glob]
    alts: list[list[_Token]] = [[]]
    for part in parts:
        if part == "**/":
            alts = [a for a in alts] + [[*a, ("**", ""), ("c", "/")] for a in alts]
            continue
        flat = _flat(part)
        alts = [[*a, *flat] for a in alts]
    return tuple(tuple(a) for a in alts)


def _flat(glob: str) -> list[_Token]:
    out: list[_Token] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**", i):
            out.append(("**", ""))
            i += 2
            continue
        if c == "*":
            out.append(("*", ""))
        elif c == "?":
            out.append(("?", ""))
        elif c == "[" and (end := glob.find("]", i + 2)) > 0 and "/" not in glob[i:end]:
            body = glob[i + 1 : end]
            neg = body[:1] in "!^" and len(body) > 1
            body = body[1:] if neg else body
            try:
                rx = re.compile("[" + ("^" if neg else "") + body.replace("\\", "\\\\").replace("[", "\\[") + "]")
            except re.error:
                out.append(("c", c))
            else:
                out.append(("cls", rx.pattern))
                i = end
        else:
            out.append(("c", c))
        i += 1
    return out


def literal(path: str) -> tuple[tuple[_Token, ...], ...]:
    """A path with nothing in it read as a pattern: every character is itself."""
    return (tuple(("c", ch) for ch in path),)


def reaches(items: list[_Item], alts: tuple[tuple[_Token, ...], ...], caseless: bool = False) -> bool:
    """Is there a string that the word can spell (each None standing for any string, slashes included) and that the glob
    matches in full? `alts` is `tokens(glob)` or `literal(path)`."""
    if len(items) > MAX_WORD:
        return True
    return any(_reach(items, toks, caseless) for toks in alts)


def _reach(items: list[_Item], toks: tuple[_Token, ...], caseless: bool) -> bool:
    n, m = len(toks), len(items)
    seen: set[tuple[int, int]] = set()
    todo = [(0, 0)]
    while todo:
        i, j = todo.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == n and j == m:
            return True
        if j < m and items[j] is None:
            todo.append((i, j + 1))  # the unresolved part ends here...
            if i < n:
                todo.append((i + 1, j))  # ...or takes the next token of the glob into itself
            continue
        if i < n and toks[i][0] in ("*", "**"):
            todo.append((i + 1, j))
        if j < m and i < n:
            ch = items[j]
            assert ch is not None
            kind, text = toks[i]
            if kind == "c":
                if (ch.casefold() == text.casefold()) if caseless else ch == text:
                    todo.append((i + 1, j + 1))
            elif kind == "?":
                if ch != "/":
                    todo.append((i + 1, j + 1))
            elif kind == "*":
                if ch != "/":
                    todo.append((i, j + 1))
            elif kind == "**":
                todo.append((i, j + 1))
            elif kind == "cls" and ch != "/" and re.fullmatch(text, ch, re.IGNORECASE if caseless else 0):
                todo.append((i + 1, j + 1))
    return False
