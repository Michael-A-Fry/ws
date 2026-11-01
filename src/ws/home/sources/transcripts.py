"""Tokens per session, from the `usage` fields of Claude Code's transcripts and nothing else in them.

A transcript is `<claude dir>/projects/<folder>/<session id>.jsonl`. This source finds the file of each session earlier
sources named, and adds up `message.usage` over its lines (a message that appears on several lines counts once, at its
last usage). It reads no other field. It never opens the transcript of an opaque session, and a transcript it can't read
or that is too large leaves that session's tokens unmeasured rather than failing the source.
"""

from __future__ import annotations

import re

from ws.home.records import Session
from ws.home.sources import SourceError, _files

NAME = "transcripts"
PROVIDES = ("sessions",)
ORDER = 70
RUNNER = "claude-code"
MAX_BYTES = 256 * 1024 * 1024
LINE_BYTES = 16 * 1024 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9-]{1,64}$")  # a session id that can't carry a glob or a path
_FIELDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
    ("cache_read", "cache_read_input_tokens"),
)


def available(env):
    folder = env.path("claude_dir") / "projects"
    return (True, "") if folder.is_dir() else (False, "no projects folder in the Claude Code folder")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def tokens_of(path) -> dict | None:
    """The token totals of one transcript from its `usage` fields; None when it has none."""
    by_message: dict[str, dict] = {}
    for n, line in enumerate(_files.iter_jsonl(path, MAX_BYTES, LINE_BYTES)):
        message = line.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        key = message.get("id") if isinstance(message.get("id"), str) else f"line-{n}"
        by_message[key] = {name: _count(usage.get(field)) for name, field in _FIELDS}
    if not by_message:
        return None
    return {name: sum(m[name] for m in by_message.values()) for name, _ in _FIELDS}


def _file(env, sid):
    for path in sorted((env.path("claude_dir") / "projects").glob(f"*/{sid}.jsonl")):
        return path
    return None


def read(kind, env):
    out = []
    for s in env.sessions_so_far():
        if s.runner != RUNNER or s.opaque or not _SAFE.match(s.id):
            continue
        path = _file(env, s.id)
        if path is None:
            continue
        try:
            totals = tokens_of(path)
        except SourceError:
            continue
        if totals is not None:
            out.append(Session(id=s.id, source=NAME, runner=RUNNER, tokens=totals))
    return out
