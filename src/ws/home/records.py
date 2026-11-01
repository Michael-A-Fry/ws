"""The home records: frozen dataclasses with a `to_dict`, and the opaque rule that hides what a person must open to see.

A source returns these; the API merges them and calls `seal` last. Nothing here reads a file: the rule is a pure
function of a record and the folders the person marked opaque. The envelope is `ws.home/1`; it only gains fields.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

SCHEMA = "ws.home/1"

STATES = ("working", "waiting", "idle", "ended", "unknown")
ASK_KINDS = ("question", "permission", "approval", "waiting", "draft", "plugin")
ANSWER_IN = ("app", "terminal")
NOT_MEASURED = "not measured"
HIDDEN = "hidden"


def _plain(value: Any) -> Any:
    """A value as JSON holds it: tuples become lists, nested records become dicts."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class _Record:
    def to_dict(self) -> dict:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}  # type: ignore[arg-type]


@dataclass(frozen=True)
class Session(_Record):
    """One running or finished session, from any runner. `id` is the runner's own session id."""

    id: str
    source: str
    runner: str = ""
    name: str = ""
    host: str = ""
    tmux: str = ""
    cwd: str = ""
    repo: str = ""
    state: str = "unknown"  # one of STATES
    waiting_for: str = ""
    started_at: str | None = None  # RFC 3339, UTC, with a Z
    last_seen: str | None = None
    tokens: dict | None = None  # input, output, cache_creation, cache_read; None = not measured
    cost_usd: float | None = None
    cost_basis: str = NOT_MEASURED  # reported, otel, or NOT_MEASURED; HIDDEN on an opaque session
    lead: bool = False
    opaque: bool = False
    opens: tuple = ()  # filled by the openers (ws.home.open), never by a source
    rc_url: str = ""  # a link the source gave for the Claude app; never built from parts


@dataclass(frozen=True)
class Ask(_Record):
    """Something waiting for a person: a question, a permission, an approval, a draft."""

    id: str
    source: str
    kind: str  # one of ASK_KINDS
    session_id: str = ""
    session_name: str = ""
    tmux: str = ""
    repo: str = ""
    title: str = ""
    options: tuple = ()  # {label, description}; the first is the recommended one
    decision: str = ""
    created: str | None = None
    opaque: bool = False
    answer_in: str = "terminal"  # one of ANSWER_IN
    opens: tuple = ()
    rc_url: str = ""


@dataclass(frozen=True)
class EvalArm(_Record):
    """One eval arm: the report metrics `ws eval report` shows, in the shape a page reads."""

    arm: str
    job: str = ""
    trials: int = 0
    solved: int = 0
    pass_rate: float | None = None
    pass_hat_k: dict = field(default_factory=lambda: {"k": None, "value": None})
    time_per_solved: dict = field(default_factory=lambda: {"p50_s": None, "p90_s": None})
    tokens_per_solved: float | None = None
    cost_per_solved_usd: float | None = None
    trend: tuple = ()  # {batch, ts, n, pass_rate}, oldest first
    noise: dict = field(default_factory=dict)  # points (a gap under this is noise), wins_needed
    not_measured: dict = field(default_factory=dict)  # metric -> why


@dataclass(frozen=True)
class Plugin(_Record):
    name: str
    on: bool = False
    built: bool = False
    region_ok: bool = True
    asks: int = 0


@dataclass(frozen=True)
class Event(_Record):
    """One event, as the source recorded it. `summary` is built from the event's labels, never from its content."""

    id: str
    ts: str
    source: str
    type: str
    session_id: str = ""
    summary: str = ""
    opaque: bool = False


@dataclass(frozen=True)
class SourceRow(_Record):
    """One source in `sources[]`: read whole (`ok`), or why not. `kind` is the typed reason (`ok` when it was read)."""

    name: str
    ok: bool
    kind: str = "ok"
    why: str = ""
    n: int = 0  # records it gave


@dataclass(frozen=True, kw_only=True)
class Snapshot(_Record):
    """Everything the home shows at one moment. `lead` is also the one session in `sessions` that has `lead` set."""

    schema: str = SCHEMA
    ts: str
    host: str
    lead: Session | None = None
    asks: tuple = ()
    sessions: tuple = ()
    plugins: tuple = ()
    evals: tuple = ()
    events: tuple = ()
    sources: tuple = ()


# --- the opaque rule ----------------------------------------------------------------------------------------------

# What an opaque record keeps. Everything else goes back to its default: no name, folder, title, tokens or cost.
KEEP: dict[type, tuple[str, ...]] = {
    Session: ("id", "source", "repo", "tmux", "state", "started_at"),
    Ask: ("id", "source", "kind", "repo", "tmux", "created"),
    Event: ("id", "source", "type", "ts"),
}


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def roots_of(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The folders a person marked opaque, as absolute paths: the path as written and with links followed."""
    out: list[str] = []
    for p in paths:
        for form in (_norm(p), os.path.realpath(os.path.expanduser(p))):
            if form not in out:
                out.append(form)
    return tuple(out)


def under(path: str, roots: tuple[str, ...]) -> bool:
    """True when `path` is one of `roots` or inside one. A path ws can't turn into an absolute one counts as under."""
    if not path:
        return False
    try:
        forms = (_norm(path), os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return True
    return any(f == r or f.startswith(r.rstrip(os.sep) + os.sep) for f in forms for r in roots)


def label_matches(label: str, roots: tuple[str, ...]) -> bool:
    """True when a repo label is the last folder name of an opaque root (a label has no folder to test)."""
    return bool(label) and any(os.path.basename(r.rstrip(os.sep)) == label for r in roots)


def is_opaque(rec: Session | Ask | Event, roots: tuple[str, ...], session_opaque: bool = False) -> bool:
    """Fail closed: opaque when the source said so, the folder or repo is under an opaque root, the session it belongs
    to is opaque, or ws can't tell where it belongs. `session_opaque` is the verdict on the record's session."""
    if rec.opaque or session_opaque:
        return True
    if isinstance(rec, Session):
        return under(rec.cwd, roots) or label_matches(rec.repo, roots) or not (rec.cwd or rec.repo)
    if isinstance(rec, Ask):
        placed = rec.repo or rec.session_id
        return under(rec.repo, roots) or label_matches(rec.repo, roots) or not placed
    return not rec.session_id  # an event: it belongs to a session or it can't be placed


def seal(rec: Session | Ask | Event, opaque: bool) -> Session | Ask | Event:
    """The record as it leaves the API: whole, or reduced to KEEP with `opaque` set."""
    if not opaque:
        return rec
    keep = KEEP[type(rec)]
    shown = {name: getattr(rec, name) for name in keep}
    if isinstance(rec, Session):
        shown["cost_basis"] = HIDDEN
    return type(rec)(**shown, opaque=True)  # type: ignore[arg-type]
