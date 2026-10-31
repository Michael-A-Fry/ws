"""YAML front matter, the safe subset: stdlib only.

Reads and writes `---` fenced blocks of `key: value` lines where a value is a scalar (plain or quoted string,
number, true/false, null, ISO date or datetime) or a flat list (`[a, b]` or `- a` lines). Anything else (nesting,
block scalars, anchors, multi-line values) is not understood: it is kept verbatim in `Document.unparsed`, the file
still reads, and nothing crashes. ws never rewrites a file it did not write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*)|[ \t]*)$")
_ITEM = re.compile(r"^[ \t]*-(?:[ \t]+(.*)|[ \t]*)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}(:?\d{2})?)?$")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ./@+-]*$")
_LOOKS_SPECIAL = re.compile(r"^(true|false|yes|no|on|off|null|y|n)$", re.IGNORECASE)
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r", "/": "/", "0": "\0", " ": " "}


class _Complex(Exception):
    """A value outside the safe subset."""


@dataclass
class Document:
    meta: dict = field(default_factory=dict)  # the keys that were understood, in file order
    body: str = ""
    raw: str | None = None  # the whole front matter block, verbatim; None when the file has none
    unparsed: list[str] = field(default_factory=list)  # chunks outside the safe subset, verbatim

    @property
    def clean(self) -> bool:
        """True when every line of the front matter was understood."""
        return not self.unparsed


def _unquote_double(s: str) -> tuple[str, str]:
    """Read a "double quoted" string from the start of s. Returns (value, rest after the closing quote)."""
    out: list[str] = []
    i = 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return "".join(out), s[i + 1 :]
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            e = s[i]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
            elif e in "ux":
                width = 4 if e == "u" else 2
                digits = s[i + 1 : i + 1 + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise _Complex
                out.append(chr(int(digits, 16)))
                i += width
            else:
                raise _Complex
        else:
            out.append(c)
        i += 1
    raise _Complex  # no closing quote


def _unquote_single(s: str) -> tuple[str, str]:
    out: list[str] = []
    i = 1
    while i < len(s):
        if s[i] == "'":
            if s[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), s[i + 1 :]
        out.append(s[i])
        i += 1
    raise _Complex


def _only_comment(rest: str) -> bool:
    rest = rest.strip()
    return not rest or rest.startswith("#")


def _scalar(text: str):
    s = text.strip()
    if s.startswith(('"', "'")):
        value, rest = _unquote_double(s) if s[0] == '"' else _unquote_single(s)
        if not _only_comment(rest):
            raise _Complex
        return value
    if re.search(r"(^|[ \t])#", s):
        s = re.split(r"(?:^|[ \t])#", s, maxsplit=1)[0].strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s[0] in "&*!|>%@`{}[]," or ": " in s or s.endswith(":"):
        raise _Complex
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    if _DATE.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return s
    if _DATETIME.match(s):
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return s
    return s


def _split_flow(inner: str) -> list[str]:
    """Split the inside of [a, "b, c", 'd'] on commas that are not in quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(inner):
        c = inner[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < len(inner):
                i += 1
                buf.append(inner[i])
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
            buf.append(c)
        elif c == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if quote:
        raise _Complex
    tail = "".join(buf)
    if tail.strip() or parts:
        parts.append(tail)
    return parts


def _flow_list(text: str) -> list:
    s = text.strip()
    close = s.rfind("]")
    if close < 0 or not _only_comment(s[close + 1 :]):
        raise _Complex
    items = _split_flow(s[1:close])
    if items and not items[-1].strip():  # a trailing comma is allowed
        items.pop()
    out = []
    for item in items:
        v = _scalar(item)
        if v is None and not item.strip():
            raise _Complex
        out.append(v)
    return out


def _entries(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """Group lines into top-level entries: a key line plus the indented, blank or `- ` lines that follow it."""
    groups: list[tuple[str | None, list[str]]] = []
    for line in lines:
        m = _KEY.match(line)
        if m and not line[0].isspace():
            groups.append((m.group(1), [line]))
        elif groups and (not line.strip() or line[0] in " \t" or _ITEM.match(line)):
            groups[-1][1].append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            groups.append((None, [line]))
    return groups


def _entry_value(lines: list[str]):
    m = _KEY.match(lines[0])
    assert m  # callers only pass key entries
    inline = (m.group(2) or "").strip()
    rest = [ln for ln in lines[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    if inline and not inline.startswith("#"):
        if rest:
            raise _Complex  # a value that continues over several lines
        if inline.startswith("["):
            return _flow_list(inline)
        return _scalar(inline)
    if not rest:
        return None
    items = []
    for ln in rest:
        im = _ITEM.match(ln)
        if not im:
            raise _Complex
        v = _scalar(im.group(1) or "")
        if v is None and not (im.group(1) or "").strip():
            raise _Complex
        items.append(v)
    return items


def parse(text: str) -> Document:
    """Split a file into front matter and body. Never raises."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return Document(body=text)
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "...")), None)
    if end is None:
        return Document(body=text)
    block = lines[1:end]
    body_lines = lines[end + 1 :]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]  # the one blank line dumps() puts after the fence
    doc = Document(body="\n".join(body_lines), raw="\n".join(block))
    for key, group in _entries(block):
        if key is None:
            doc.unparsed.append("\n".join(group).rstrip("\n"))
            continue
        try:
            doc.meta[key] = _entry_value(group)
        except _Complex:
            doc.unparsed.append("\n".join(group).rstrip("\n"))
    return doc


def _quote(s: str) -> str:
    out = []
    for c in s:
        o = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029, 0xFEFF):
            out.append(f"\\u{o:04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _string(s: str) -> str:
    plain = (
        _PLAIN_SAFE.match(s)
        and s == s.strip()
        and not _LOOKS_SPECIAL.match(s)
        and not _INT.match(s)
        and not _FLOAT.match(s)
        and not _DATE.match(s)
        and not _DATETIME.match(s)
    )
    return s if plain else _quote(s)


def _value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime)):  # datetime is a date, so one branch writes both
        return v.isoformat()
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, (list, tuple, dict)) for x in v):
            raise TypeError("front matter lists must be flat")
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to front matter")


def dumps(meta: dict, body: str = "", unparsed: list[str] | None = None) -> str:
    """Write front matter and body. Chunks passed as `unparsed` are written back verbatim after the keys."""
    lines = []
    for k, v in meta.items():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", k):
            raise ValueError(f"not a safe front matter key: {k!r}")
        lines.append(f"{k}: {_value(v)}")
    lines.extend(unparsed or [])
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body.rstrip("\n") + "\n"
