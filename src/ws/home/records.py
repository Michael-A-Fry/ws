"""The home records: frozen dataclasses with a `to_dict`, and the opaque rule that hides what a person must open to see.

A source returns these; the API merges them and calls `seal` last. Nothing here reads a file: the rule is a pure
function of a record and the folders the person marked opaque. The envelope is `ws.home/1`; it only gains fields.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
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


def _internal() -> Any:
    """A field that stays inside ws: the API reads it, `to_dict` leaves it out."""
    return field(default=False, metadata={"internal": True})


class _Record:
    def to_dict(self) -> dict:
        return {
            f.name: _plain(getattr(self, f.name))
            for f in fields(self)  # type: ignore[arg-type]
            if not f.metadata.get("internal")
        }


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
    placed: bool = (
        _internal()
    )  # the source itself saw an open folder for this ask (not output); a file's word isn't that


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
    placed: bool = _internal()  # as Ask.placed


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

# What an opaque record keeps. Everything else goes back to its default: no name, folder, title, tokens or cost. `repo`
# and `tmux` are in the list but pass through a check in `seal`: the repo label only when the person listed it as
# showable, the tmux target only as a bare id (else a hash).
KEEP: dict[type, tuple[str, ...]] = {
    Session: ("id", "source", "repo", "tmux", "state", "started_at"),
    Ask: ("id", "source", "kind", "repo", "tmux", "created"),
    Event: ("id", "source", "type", "ts"),
}


_HEX = r"[0-9A-Fa-f]"
_UUID = rf"{_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}"
# the id shapes ws's own sources make: a UUID, `waiting:<UUID>`, an ask id (`a20261102T120000-k3x9`), a hex string
MACHINE_ID = re.compile(rf"{_UUID}|waiting:{_UUID}|a\d{{8}}T\d{{6}}-[a-z0-9]{{4}}|{_HEX}{{8,64}}")


def _nfc(text: str) -> str:
    """Text in one Unicode form, so `café` typed two ways is one name."""
    return unicodedata.normalize("NFC", text)


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


def _inside(form: str, root: str) -> bool:
    """`form` is `root` or in it, compared the way this file system compares names (os.path.normcase)."""
    f, r = _nfc(os.path.normcase(form)), _nfc(os.path.normcase(root))
    return f == r or f.startswith(r.rstrip(os.sep) + os.sep)


def _same(a: str, b: str) -> bool:
    """True when `a` and `b` are the same folder on disk (a different spelling on a case-insensitive file system).
    A name that isn't there is not the same; any other failure to look counts as the same (fail closed)."""
    try:
        return os.path.samefile(a, b)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return True


def _beside_root(form: str, roots: tuple[str, ...]) -> bool:
    """True when `form` or a folder above it is, on disk, one of the roots: ~/Code/fiu covers ~/code/fiu on macOS."""
    cur = form
    while True:
        if os.path.lexists(cur) and any(_same(cur, r) for r in roots):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def under(path: str, roots: tuple[str, ...]) -> bool:
    """True when `path` is one of `roots` or inside one, by name or on disk. A path ws can't turn into an absolute one
    (relative, or one the system refuses) counts as under. The empty path is not under anything: whether it is
    *placed* is `open_path`'s question."""
    if not path:
        return False
    try:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            return True
        forms = (_norm(path), os.path.realpath(expanded))
        if any(_inside(f, r) for f in forms for r in roots):
            return True
        return any(_beside_root(f, roots) for f in dict.fromkeys(forms))
    except (OSError, ValueError):
        return True


def open_path(path: str, roots: tuple[str, ...]) -> bool:
    """True only when ws knows where something is and that place isn't opaque: a non-empty absolute path that isn't
    under a root. Empty, relative, unreadable or under a root is False: only a known open place clears a record."""
    return bool(path) and os.path.isabs(os.path.expanduser(path)) and not under(path, roots)


def label_matches(label: str, roots: tuple[str, ...]) -> bool:
    """True when a repo label is the last folder name of an opaque root (a label has no folder to test)."""
    if not label:
        return False

    def plain(name: str) -> str:
        return _nfc(" ".join(_nfc(name).split())).casefold()

    want = plain(label)
    return any(plain(os.path.basename(r.rstrip(os.sep))) == want for r in roots)


def part_denied(rec: Session, roots: tuple[str, ...]) -> bool:
    """One source's piece of a session says it is opaque: marked so, a folder that isn't open, or an opaque repo label.
    A piece with no folder (tokens, cost) says nothing; the merged session still needs a known open folder."""
    return rec.opaque or label_matches(rec.repo, roots) or (bool(rec.cwd) and not open_path(rec.cwd, roots))


def is_opaque(rec: Session | Ask | Event, roots: tuple[str, ...], session: bool | None = None) -> bool:
    """Fail closed: a record is open only when something known clears it. `session` is the verdict on the session the
    record belongs to: True opaque, False a session in the snapshot that is open, None not in the snapshot (or none).
    A session is open on a known open folder; an ask or event on an open session or a folder of its own."""
    if rec.opaque or session:
        return True
    if isinstance(rec, Session):
        return part_denied(rec, roots) or not open_path(rec.cwd, roots)
    if label_matches(getattr(rec, "repo", ""), roots):
        return True
    return not (session is False or rec.placed)


# Hashes on an opaque record are keyed (HMAC-SHA256), so a person who guesses a case number can't test it against the
# output. The API passes the per-install key (`api.seal_key`); without one, a key made fresh for this process is used,
# so a hash never depends on text alone.
_PROCESS_KEY = os.urandom(32)


def _digest(text: str, key: bytes | None = None) -> str:
    mac = hmac.new(key or _PROCESS_KEY, text.encode("utf-8", "surrogatepass"), hashlib.sha256)
    return mac.hexdigest()[:16]


def safe_id(ident: str, key: bytes | None = None) -> str:
    """An id as it may leave on an opaque record: itself when it has a machine shape (MACHINE_ID), else a 16-character
    keyed hash. A free-text id (a case number, a title) never leaves as written."""
    return ident if MACHINE_ID.fullmatch(ident) else _digest(ident, key)


# a tmux target that is only ids: `@9`, `%3`, `@9.%3`, `$1:@9.%3`. A session or window name is text, so it isn't one.
TMUX_ID = re.compile(r"(?:\$\d+:)?@\d+(?:\.%\d+)?|%\d+|\$\d+")
# the event types an opaque event may show as written: the kinds a ws record already names.
EVENT_TYPES = (*ASK_KINDS, *STATES)


def source_names() -> frozenset[str]:
    """The names of the source modules in `ws.home.sources`, read from the folder without importing any."""
    import pkgutil

    from ws.home import sources

    return frozenset(i.name for i in pkgutil.iter_modules(sources.__path__) if not i.name.startswith("_"))


def safe_tmux(target: str, key: bytes | None = None) -> str:
    """A tmux target as it may leave on an opaque record: empty stays empty, a bare id stays, anything else (a window
    named for its work, a session name) becomes a 16-character keyed hash."""
    if not target or TMUX_ID.fullmatch(target):
        return target
    return _digest(target, key)


def safe_source(text: str, key: bytes | None = None) -> str:
    """A source as it may leave on an opaque record: the name of a source module (or several joined by `+`), else a
    16-character keyed hash. A source name read from a file is text, however short."""
    known = source_names()
    return text if text and all(part in known for part in text.split("+")) else _digest(text, key)


def safe_type(text: str, key: bytes | None = None) -> str:
    """An event type as it may leave on an opaque record: one of EVENT_TYPES, else a 16-character keyed hash."""
    return text if text in EVENT_TYPES else _digest(text, key)


def showable(label: str, show_repos: tuple[str, ...]) -> bool:
    """True when a repo label is one the person listed as showable (`[home] show_repos`): the whole label, compared
    without regard to case or Unicode form."""
    want = _nfc(" ".join(label.split())).casefold()
    return bool(want) and any(_nfc(" ".join(r.split())).casefold() == want for r in show_repos)


def seal(
    rec: Session | Ask | Event, opaque: bool, show_repos: tuple[str, ...] = (), key: bytes | None = None
) -> Session | Ask | Event:
    """The record as it leaves the API: whole, or reduced to KEEP with `opaque` set. The id is made safe; the tmux
    target is kept as a bare id or a keyed hash; the repo label is kept only when `show_repos` lists it; a state or ask
    kind outside its list, a source that isn't a source module and a type outside EVENT_TYPES don't survive as written."""
    if not opaque:
        return rec
    keep = KEEP[type(rec)]
    shown = {name: getattr(rec, name) for name in keep}
    shown["id"] = safe_id(shown["id"], key)
    shown["source"] = safe_source(shown["source"], key)
    if "repo" in shown and not showable(shown["repo"], show_repos):
        shown["repo"] = ""
    if "tmux" in shown:
        shown["tmux"] = safe_tmux(shown["tmux"], key)
    if isinstance(rec, Session):
        shown["cost_basis"] = HIDDEN
        if shown["state"] not in STATES:
            shown["state"] = "unknown"
    elif isinstance(rec, Ask):
        if shown["kind"] not in ASK_KINDS:
            shown["kind"] = "question"
    else:
        shown["type"] = safe_type(shown["type"], key)
    return type(rec)(**shown, opaque=True)  # type: ignore[arg-type]
