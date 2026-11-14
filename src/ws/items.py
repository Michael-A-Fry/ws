"""One item layer over the stores ws already has: a `kind:id` reference, one table of kinds, `get`, `list`, a rebuilt link index and one checked change path.

Every thing ws keeps (a run, a rating, a session, a queue receipt, a message envelope, an area, a register row) is an item
named `kind:id`. `KINDS` is the one table that says, per kind, the owning module, the store, the id pattern, whether the
store is the truth or derived, the write rule (`append-only`, `gated` or `single writer`) and the reader. Nothing is
moved: `get` and `list` go through a reader over the store as it is, and a body-free reader never shows a message body, a
command or a question. The link index is a SQLite table `(from_ref, rel, to_ref)` that is deleted and rebuilt from the
stores on each run and never patched. `change` is the one write path for a gated kind: re-read, apply, validate, write by
temp file and rename under a lock only when the item's revision (sha256 of its bytes) is the one the caller saw, else
hand back the fresh state. Saved views are TOML records resolved at read time. `docs/reference/items.md` has the model
and the add-a-kind guide. Ideas only from Macro (AGPL); no code.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ws import jsonl, paths
from ws.output import WsError

_LIST = builtins.list  # this module defines `list`, the public name; its own code uses this one

WRITE_RULES = ("append-only", "gated", "single writer")
TRUTH = ("truth", "derived")
RELS = ("supersedes", "source", "about", "used", "added", "produced", "parent")
LIST_MAX = 300
TRIES = 3
VIEW_VERSION = 1
PERSON = "person:"  # an actor that starts with this is a person; any other actor is an agent
_REF = re.compile(r"^([a-z][a-z-]*):(\S+)$")


class ItemError(WsError):
    """A typed failure: `kind` is `bad-ref`, `unknown-kind`, `bad-field`, `bad-view`, `not-gated` or `store`."""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(kind, message, hint, 1)


@dataclass(frozen=True)
class Kind:
    name: str
    module: str  # the module that owns the store
    stores: tuple[str, ...]  # the names docs/reference/stores.md gives the store
    id_pattern: str  # a regex the id must match in full
    truth: str  # truth or derived
    write: str  # append-only, gated or single writer
    reader: Callable[[dict], Iterable[dict]] | None  # env -> rows, each with an `id`
    strict: bool = False  # a link that does not resolve is a fault, not a rotated-away record
    human_only: tuple[str, ...] = ()  # fields only a person may change (gated kinds)


def _env(env: dict | None) -> dict:
    return dict(os.environ if env is None else env)


# --- readers: each goes through the owning module's reader and never shows a body -----------------------------------------


def _hid(rec: dict) -> str:
    blob = json.dumps(rec, sort_keys=True, ensure_ascii=True, default=str)
    return "h" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def _log(path_of: Callable[[dict], Path], keep: tuple[str, ...] | None = None, key: str | None = None):
    """A reader over a JSON-lines log (all rotated files). `keep` narrows each line to the named keys (a log that holds a
    command or a question shows none of it); `key` names the field that is the id, else the id is a hash of the line."""

    def read(env: dict) -> Iterable[dict]:
        p = path_of(env)  # the log's path without its suffix: this module reads logs and names none
        p = p.with_name(p.name + ".jsonl")
        try:
            recs = jsonl.read(p)
        except OSError as e:
            raise ItemError("store", f"cannot read {p.name}: {type(e).__name__}", "check the file") from e
        for rec in recs:
            row = {k: v for k, v in rec.items() if keep is None or k in keep}
            row["id"] = str(rec[key]) if key and rec.get(key) is not None else _hid(row)
            yield row

    return read


def _ASKS(env: dict) -> Path:
    return Path(env.get("HOME", "~")).expanduser() / ".claude-usage" / "asks" / "asks"


def _state(*parts: str) -> Callable[[dict], Path]:
    return lambda env: paths.state_dir(env).joinpath(*parts)


def _files(dir_of: Callable[[dict], Path], keep: tuple[str, ...], suffix: str = ".json"):
    """A reader over a folder of JSON record files, named `<id>.json`; only the `keep` keys are shown."""

    def read(env: dict) -> Iterable[dict]:
        d = dir_of(env)
        if not d.is_dir():
            return
        for p in sorted(d.glob("*" + suffix)):
            if p.name.count(".") != 1 or p.is_symlink():  # `<id>.<guard>.json` is a guard record, not a session
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                yield {"id": p.stem, **{k: v for k, v in doc.items() if k in keep}}

    return read


def _runs(env: dict) -> Iterable[dict]:
    from ws import runrecord

    for rec in runrecord.read(runrecord.runs_path(env)):
        if rec.get("run_id"):
            yield {**rec, "id": rec["run_id"]}


def _ratings(env: dict) -> Iterable[dict]:
    from ws import rating

    for rec in rating.read(rating.ratings_path(env)):
        yield {**rec, "id": f"{rec.get('run_id')}@{rec.get('ts')}"}


def _receipts(env: dict) -> Iterable[dict]:
    from ws import queue

    if not (paths.state_dir(env) / "queue" / "queue.db").exists():
        return
    for r in queue.ledger_for_env(env).receipts(last=100000):
        yield {**r, "id": r["run_id"]}


def _messages(env: dict) -> Iterable[dict]:
    from ws import msg

    after = 0
    while True:  # the body-free feed, a page at a time: never a body
        page = msg.envelopes(after=after, limit=200)
        for e in page:
            yield {**e, "id": e["message_id"]}
        if len(page) < 200:
            return
        after = page[-1]["seq"]


def _sessions(env: dict) -> Iterable[dict]:
    from ws import session

    for r in session.records(env):
        yield {
            "id": r.session_id,
            "runner": r.runner,
            "release": r.release,
            "started_at": r.started_at,
            "role": r.role,
            "class": r.class_,
        }


def _approvals(env: dict) -> Iterable[dict]:
    root = paths.state_dir(env) / "approvals"
    for state in ("pending", "granted"):  # ids and times only: the bound command stays in the file
        for row in _files(lambda e, s=state: root / s, ("schema", "ts", "created", "granted"))(env):
            yield {**row, "state": state}


def _versions(env: dict) -> Iterable[dict]:
    p = paths.state_dir(env) / "versions.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(doc, dict):
        yield {"id": "current", "schema": doc.get("schema"), "default_model": doc.get("default_model")}


def _limits(env: dict) -> Iterable[dict]:
    p = paths.state_dir(env) / "usage" / "rate_limits.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(doc, dict):
        yield {"id": "current", "schema": doc.get("schema"), "captured_at": doc.get("captured_at")}


def _guard_records(env: dict) -> Iterable[dict]:
    d = paths.state_dir(env) / "sessions"
    if d.is_dir():
        for p in sorted(d.glob("*.*.json")):  # ids only: the contents are counts and message ids
            if not p.is_symlink():
                yield {"id": p.name[: -len(".json")], "session": p.name.split(".")[0], "guard": p.name.split(".")[1]}


def _areas_dir(env: dict) -> Path:
    return paths.config_dir(env) / "areas"


def _read_area(p: Path) -> dict | None:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _areas(env: dict) -> Iterable[dict]:
    d = _areas_dir(env)
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        doc = _read_area(p)
        if doc is not None:
            yield {**doc, "id": p.stem}


def _views_dir(env: dict) -> Path:
    return paths.config_dir(env) / "views"


def _views(env: dict) -> Iterable[dict]:
    d = _views_dir(env)
    for p in sorted(d.glob("*.toml")) if d.is_dir() else []:
        try:
            doc = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"error": "unreadable"}
        yield {"id": p.stem, **{k: v for k, v in doc.items() if k in ("kind", "version", "sort", "group")}}


# One register parser. A row is a Markdown task line whose first word is its id; fields ride in a trailing `{k=v ...}`.
_ROW = re.compile(r"^\s*[-*] \[([ xX])\] ([A-Za-z]+-?\d+) (.*?)\s*(?:\{([^{}]*)\})?\s*$")
ROW_FIELDS = ("area", "due", "needs", "every", "who", "chase_on")


def parse_register(text: str) -> list[dict]:
    """The rows of a register file. A task line with no id word is not an item. Fields: `area` (an area id), `due`,
    `chase_on` (dates), `every` (a period such as 7d), `who`, and `needs` (comma-separated row ids)."""
    rows = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        row = {"id": m.group(2), "done": m.group(1) != " ", "text": m.group(3)}
        for tok in (m.group(4) or "").split():
            k, _, v = tok.partition("=")
            if k in ROW_FIELDS and v:
                row[k] = v
        rows.append(row)
    return rows


def _rows(env: dict) -> Iterable[dict]:
    p = Path(env.get("WS_REGISTER") or Path.cwd() / "TODO.md")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    yield from parse_register(text)


# --- the table -----------------------------------------------------------------------------------------------------------

_RUN = r"[0-9A-Za-z_.:-]+"
_HASH = r"h[0-9a-f]{12}"
_SID = r"[0-9A-Za-z_-]+"
_DATA = lambda *parts: lambda env: paths.data_dir(env).joinpath(*parts)


def _row(name, module, store, pattern, write, reader, truth="truth", **more) -> tuple[str, Kind]:
    stores = (store,) if isinstance(store, str) else store
    return name, Kind(name, module, stores, pattern, truth, write, reader, **more)


def _logrow(name, module, store, where, keep=None, key=None) -> tuple[str, Kind]:
    return _row(name, module, store, r"\S+" if key else _HASH, "append-only", _log(where, keep, key))


KINDS: dict[str, Kind] = dict(
    (
        _row("run", "runrecord", ("Run records", "Run records, fallback"), _RUN, "append-only", _runs),
        _row("rating", "rating", "Ratings", r"\S+@\S+", "append-only", _ratings),
        _logrow("canary", "canary", "Canary", _state("logs", "canary")),
        _logrow("eval-result", "eval", "Eval results", _DATA("eval", "results")),
        _logrow("eval-look", "eval", "Eval looks", _DATA("eval", "looks")),
        # never the payload, the claims text, the question or the broker's host:
        _logrow(
            "gate-call",
            "guards.record_calls",
            "Gate calls",
            _state("logs", "gate-calls"),
            ("schema", "ts", "at", "runner"),
        ),
        _logrow(
            "claim",
            "guards.claims",
            "Claims",
            _state("logs", "claims"),
            ("schema", "ts", "at", "runner", "session", "ref"),
        ),
        _logrow("verdict", "gate", "Gate verdicts", _state("logs", "gate")),
        _logrow("wall-report", "sandbox", "Wall report", _state("logs", "wall-report")),
        _logrow(
            "footprint",
            "node",
            "Node footprint",
            _state("node", "footprint"),
            ("schema", "ts", "type", "node", "op", "step", "kind"),
        ),
        _logrow(
            "ask", "adapters.opencode", "OpenCode asks", _ASKS, ("schema", "ts", "event", "id", "repo", "session"), "id"
        ),
        _logrow(
            "broker",
            "broker",
            "Broker audit",
            _state("logs", "broker"),
            ("t", "op", "name", "purpose", "status", "bytes"),
        ),
        _row("session", "session", "Session", _SID, "single writer", _sessions),
        _row(
            "identity",
            "session",
            "Session identity",
            _SID,
            "single writer",
            _files(_state("identity"), ("schema", "role", "started_at", "ts")),
        ),
        _row("guard-record", "guards", "Guard record", r"[0-9A-Za-z_.-]+", "single writer", _guard_records),
        _row("approval", "approvals", "Approval, pending and granted", r"\S+", "single writer", _approvals),
        _row("versions", "canary", "Seen versions", "current", "single writer", _versions),
        _row("limits", "usage", "Subscription limits", "current", "single writer", _limits, "derived"),
        _row("receipt", "queue", "The queue database", _RUN, "single writer", _receipts),
        _row("message", "msg", "The messages database", r"\S+", "append-only", _messages),
        _row("area", "items", "Areas", r"a[0-9]+", "gated", _areas, strict=True, human_only=("rank", "people")),
        _row("view", "items", "Saved views", r"[a-z][a-z0-9-]*", "single writer", _views, strict=True),
        _row("row", "items", "Register", r"[A-Za-z]+-?\d+", "single writer", _rows, strict=True),
    )
)

# What each kind points at: `(rel, target kind, field)`; a field holding a comma-separated list gives one link per
# name. Every kind has a key, `()` for one that points at nothing: a kind with no key yields no links and says nothing, which
# `registry_gaps` catches.
LINK_SPEC: dict[str, tuple[tuple[str, str, str], ...]] = {
    "run": (("parent", "session", "session_id"), ("parent", "run", "parent_run_id")),
    "rating": (("about", "run", "run_id"),),
    "receipt": (("produced", "run", "run_id"),),
    "message": (("source", "session", "from"), ("about", "session", "to")),
    "claim": (("about", "session", "session"),),
    "guard-record": (("about", "session", "session"),),
    "identity": (("about", "session", "id"),),
    "ask": (("about", "session", "session"),),
    "row": (("about", "area", "area"), ("used", "row", "needs")),
    "area": (("supersedes", "area", "supersedes"),),
    **{k: () for k in ("canary", "eval-result", "eval-look", "gate-call", "verdict", "wall-report", "footprint")},
    **{k: () for k in ("broker", "session", "approval", "versions", "limits", "view")},  # points at nothing, said
}


def _links_of(kind: str):
    def links(row: dict) -> Iterable[tuple[str, str]]:
        for rel, target, field in LINK_SPEC.get(kind, ()):
            for name in str(row.get(field) or "").split(","):
                if name.strip():
                    yield rel, f"{target}:{name.strip()}"

    return links


LINKS: dict[str, Callable[[dict], Iterable[tuple[str, str]]]] = {k: _links_of(k) for k in KINDS}


# --- refs, get, list -----------------------------------------------------------------------------------------------------


def parse_ref(ref: str) -> tuple[str, str]:
    m = _REF.match(ref) if isinstance(ref, str) else None
    if not m:
        raise ItemError("bad-ref", f"not a reference: {ref!r}", "a reference is kind:id, such as run:20261101T0000Z-ab")
    return m.group(1), m.group(2)


def _kind(name: str, kinds: dict[str, Kind]) -> Kind:
    k = kinds.get(name)
    if k is None:
        raise ItemError("unknown-kind", f"no such kind: {name}", f"the kinds are {', '.join(sorted(kinds))}")
    if k.reader is None:
        raise ItemError("unknown-kind", f"kind {name} has no reader", "give its KINDS row a reader")
    return k


def get(ref: str, env: dict | None = None, kinds: dict[str, Kind] | None = None) -> dict | None:
    """The item a reference names, or None when the owning store holds no such id."""
    kinds = KINDS if kinds is None else kinds
    name, ident = parse_ref(ref)
    k = _kind(name, kinds)
    for row in k.reader(_env(env)):
        if row.get("id") == ident:
            return row
    return None


def _sort_key(field: str):
    def key(row: dict):
        v = row.get(field)
        return (v is None, str(type(v).__name__), v if v is not None else "")

    return key


def list(
    kind: str,
    where: dict | None = None,
    sort: str | None = None,
    group: str | None = None,
    env: dict | None = None,
    kinds: dict[str, Kind] | None = None,
    limit: int = LIST_MAX,
):
    """The rows of a kind through its reader. `where` keeps rows whose fields equal the given values (no operators, no
    expressions). `sort` is a field name, `-field` for descending; no sort keeps the store's order. With `group`, a dict
    from that field's value to its rows. At most `limit` rows (and never more than 300)."""
    kinds = KINDS if kinds is None else kinds
    k = _kind(kind, kinds)
    where = where or {}
    rows = [r for r in k.reader(_env(env)) if all(r.get(f) == v for f, v in where.items())]
    if sort:
        rows.sort(key=_sort_key(sort.lstrip("-")), reverse=sort.startswith("-"))
    rows = rows[: max(0, min(limit, LIST_MAX))]
    if not group:
        return rows
    out: dict = {}
    for r in rows:
        out.setdefault(r.get(group), []).append(r)
    return out


# --- saved views ---------------------------------------------------------------------------------------------------------


def load_view(path: Path) -> dict:
    """A view record `{kind, version, where, sort, group}`; a bad one is a typed error, never a silent empty list."""
    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ItemError("bad-view", f"cannot read view {Path(path).name}: {type(e).__name__}") from e
    if doc.get("version") != VIEW_VERSION or not isinstance(doc.get("kind"), str):
        raise ItemError("bad-view", f"{Path(path).name}: needs version = {VIEW_VERSION} and a kind")
    where = doc.get("where", {})
    if not isinstance(where, dict) or not all(isinstance(doc.get(k, ""), str) for k in ("sort", "group")):
        raise ItemError("bad-view", f"{Path(path).name}: where is a table; sort and group are field names")
    return {"kind": doc["kind"], "where": where, "sort": doc.get("sort") or None, "group": doc.get("group") or None}


def render_view(name: str, env: dict | None = None, kinds: dict[str, Kind] | None = None):
    """Resolve a saved view by name at read time: the same `list` call, so a new view file needs no Python change."""
    env = _env(env)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ItemError("bad-view", f"not a view name: {name!r}")
    v = load_view(_views_dir(env) / f"{name}.toml")
    return list(v["kind"], v["where"], v["sort"], v["group"], env, kinds)


# --- the link index ------------------------------------------------------------------------------------------------------


def all_links(env: dict | None = None, kinds: dict[str, Kind] | None = None) -> builtins.list[tuple[str, str, str]]:
    """Every `(from_ref, rel, to_ref)` the stores say, sorted, with no repeats."""
    kinds = KINDS if kinds is None else kinds
    env = _env(env)
    out = set()
    for name, k in kinds.items():
        if k.reader is None:
            continue
        for row in k.reader(env):
            for rel, to in LINKS.get(name, lambda r: ())(row):
                if rel not in RELS:
                    raise ItemError("bad-field", f"{name} gives the rel {rel!r}", f"a rel is one of {', '.join(RELS)}")
                out.add((f"{name}:{row['id']}", rel, to))
    return sorted(out)


def index_path(env: dict | None = None) -> Path:
    return paths.cache_dir(_env(env)) / "items" / "links.db"


def rebuild_index(env: dict | None = None, kinds: dict[str, Kind] | None = None, db: Path | None = None) -> int:
    """Delete the index and build it again from the stores, into a temp file renamed into place. Returns the row count."""
    db = db or index_path(env)
    rows = all_links(env, kinds)
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + f".{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute(
            "CREATE TABLE links (from_ref TEXT NOT NULL, rel TEXT NOT NULL, to_ref TEXT NOT NULL, "
            "PRIMARY KEY (from_ref, rel, to_ref))"
        )
        conn.execute("CREATE INDEX links_to ON links (to_ref)")
        conn.executemany("INSERT INTO links VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, db)
    return len(rows)


def index_rows(db: Path) -> builtins.list[tuple[str, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return [tuple(r) for r in conn.execute("SELECT from_ref, rel, to_ref FROM links ORDER BY 1, 2, 3")]
    finally:
        conn.close()


def linked(ref: str, db: Path, rel: str | None = None, inbound: bool = False) -> builtins.list[str]:
    """What `ref` points at (or, with `inbound`, what points at it), straight from the index."""
    col, other = ("to_ref", "from_ref") if inbound else ("from_ref", "to_ref")
    sql = f"SELECT {other} FROM links WHERE {col} = ?" + (" AND rel = ?" if rel else "") + " ORDER BY 1"
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(sql, (ref, rel) if rel else (ref,))]
    finally:
        conn.close()


# --- the checked change path ---------------------------------------------------------------------------------------------


def _rev(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _item_path(kind: Kind, ident: str, env: dict) -> Path:
    if kind.name != "area":
        raise ItemError("not-gated", f"no change path for {kind.name}", "only a gated kind with a record file has one")
    return _areas_dir(env) / f"{ident}.json"


def validate_area(doc: dict, ident: str = "", names: Iterable[str] = ()) -> builtins.list[str]:
    """The faults in an area record; none when it is good. Ids are opaque, so none holds an area's name."""
    bad = []
    if not isinstance(doc.get("name"), str) or not doc["name"].strip():
        bad.append("name is a non-empty string")
    if not isinstance(doc.get("rank"), int) or isinstance(doc.get("rank"), bool) or doc["rank"] < 0:
        bad.append("rank is a whole number, 0 or more")
    ppl = doc.get("people", [])
    if not isinstance(ppl, builtins.list) or not all(isinstance(p, str) for p in ppl):
        bad.append("people is a list of names")
    low = ident.lower()
    if ident and any(n.lower() in low for n in names if n):
        bad.append("an id holds an area's name")
    return bad


VALIDATORS: dict[str, Callable[..., builtins.list[str]]] = {"area": validate_area}


@dataclass(frozen=True)
class Change:
    outcome: str  # ok, conflict, refused, invalid, missing
    rev: str | None = None  # the item's revision now
    state: dict | None = None  # the item as it is now (the fresh state on a conflict)
    problems: tuple[str, ...] = ()


class _Lock:
    """An exclusive advisory lock on `<file>.lock`; a no-op where fcntl is absent (native Windows)."""

    def __init__(self, path: Path, timeout: float = 2.0) -> None:
        self.path, self.timeout, self.fd = path.with_name(path.name + ".lock"), timeout, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= end:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError from None
                time.sleep(0.002)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)


def read_state(
    ref: str, env: dict | None = None, kinds: dict[str, Kind] | None = None
) -> tuple[dict | None, str | None]:
    """The record and its revision (sha256 of its bytes) as they are now; `(None, None)` when there is no such item."""
    env = _env(env)
    name, ident = parse_ref(ref)
    p = _item_path(_kind(name, KINDS if kinds is None else kinds), ident, env)
    try:
        data = p.read_bytes()
        return json.loads(data), _rev(data)
    except (OSError, ValueError):
        return None, None


def _dump(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_atomic(p: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def change(
    ref: str,
    delta: dict,
    expected_rev: str,
    actor: str,
    env: dict | None = None,
    kinds: dict[str, Kind] | None = None,
    _between: Callable[[], None] | None = None,
) -> Change:
    """Apply `delta` (field -> new value; None removes) to a gated item if its revision is still `expected_rev`.

    Re-reads, applies, validates with the kind's validator, then writes by temp file and rename under a lock, only when
    the revision under the lock is the one the caller saw. A lock held too long is tried again, at most 3 times; a moved
    revision is outcome `conflict` with the fresh state and revision, never an overwrite. A field in the kind's
    `human_only` is refused unless the actor is `person:NAME`. The actor is written to `changed_by`."""
    kinds, env = (KINDS if kinds is None else kinds), _env(env)
    name, ident = parse_ref(ref)
    k = _kind(name, kinds)
    if k.write != "gated":
        raise ItemError("not-gated", f"{name} is {k.write}", "only a gated kind changes through items.change")
    if not isinstance(actor, str) or not actor:
        raise ItemError("bad-field", "a change names its actor", "person:NAME or an agent's name")
    if not isinstance(delta, dict) or "id" in delta or "changed_by" in delta:
        raise ItemError("bad-field", "a delta is a dict of fields; id and changed_by are not fields to set")
    if any(f in k.human_only for f in delta) and not actor.startswith(PERSON):
        return Change(
            "refused",
            problems=tuple(f"{f} changes only with a person as the actor" for f in delta if f in k.human_only),
        )
    p = _item_path(k, ident, env)
    for _ in range(TRIES):
        try:
            data = p.read_bytes()
        except OSError:
            return Change("missing")
        if _between:
            _between()
        try:
            with _Lock(p):
                data = p.read_bytes()  # re-read under the lock: this is the state the write is checked against
                doc = json.loads(data)
                if _rev(data) != expected_rev:
                    return Change("conflict", _rev(data), doc)
                new = {**doc, **{f: v for f, v in delta.items() if v is not None}, "changed_by": actor}
                for f, v in delta.items():
                    if v is None:
                        new.pop(f, None)
                names = [d.get("name", "") for d in _areas(env) if d["id"] != ident] if name == "area" else []
                bad = VALIDATORS[name](new, ident, names) if name in VALIDATORS else []
                if bad:
                    return Change("invalid", _rev(data), doc, tuple(bad))
                out = _dump(new)
                _write_atomic(p, out)
                return Change("ok", _rev(out), new)
        except TimeoutError:
            continue
    data = p.read_bytes()
    return Change("conflict", _rev(data), json.loads(data))


def bare_write(ref: str, delta: dict, env: dict | None = None, read: dict | None = None) -> None:
    """The comparison arm of the change trial: read, apply, write, with no revision and no lock. `read` is a copy of the
    record read earlier, so a trial can interleave two writers. Never used by ws."""
    env = _env(env)
    name, ident = parse_ref(ref)
    p = _item_path(KINDS[name], ident, env)
    doc = read if read is not None else json.loads(p.read_bytes())
    p.write_bytes(_dump({**doc, **delta}))


# --- checks --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    code: str
    where: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.where} {self.detail}".rstrip()


def check_table(kinds: dict[str, Kind] | None = None) -> builtins.list[Problem]:
    """The faults in the table itself: a row with no reader, a write rule outside the three, a bad id pattern."""
    out = []
    for name, k in (KINDS if kinds is None else kinds).items():
        if not re.fullmatch(r"[a-z][a-z-]*", name) or k.name != name:
            out.append(Problem("bad-kind-name", name))
        if not callable(k.reader):
            out.append(Problem("no-reader", name, "give the row a reader"))
        if k.write not in WRITE_RULES:
            out.append(Problem("bad-write-rule", name, f"{k.write!r} is not one of {', '.join(WRITE_RULES)}"))
        if k.truth not in TRUTH:
            out.append(Problem("bad-truth", name, f"{k.truth!r} is not truth or derived"))
        if not k.stores or not k.module:
            out.append(Problem("no-store", name, "name the module and the store"))
        try:
            re.compile(k.id_pattern)
        except re.error:
            out.append(Problem("bad-id-pattern", name))
        for f in k.human_only:
            if k.write != "gated":
                out.append(Problem("human-only-not-gated", name, f))
    return out


def check(env: dict | None = None, kinds: dict[str, Kind] | None = None) -> builtins.list[Problem]:
    """The table, then every item: an id that does not fit its kind's pattern, a strict link (a row's area or needs, a
    view's kind) that resolves to nothing, an id that holds an area's name, and an area that fails its validator."""
    kinds, env = (KINDS if kinds is None else kinds), _env(env)
    out = check_table(kinds)
    if out:
        return out  # a broken table makes the rest meaningless
    present: dict[str, set] = {}
    for name, k in kinds.items():
        try:
            rows = _LIST(k.reader(env))
        except ItemError as e:
            out.append(Problem("store", name, e.message))
            continue
        present[name] = {r["id"] for r in rows}
        pat = re.compile(k.id_pattern)
        out += [Problem("bad-id", f"{name}:{r['id']}") for r in rows if not pat.fullmatch(str(r["id"]))]
    names = [r.get("name", "") for r in _areas(env)]
    for name, k in kinds.items():
        if k.strict and name != "view":
            for r in _LIST(k.reader(env)):
                for _rel, to in LINKS.get(name, lambda x: ())(r):
                    tk, _, ti = to.partition(":")
                    if tk not in present or ti not in present[tk]:
                        out.append(Problem("dangling", f"{name}:{r['id']}", f"points at {to}, which is not there"))
                if name == "row" and any(n and n.lower() in str(r["id"]).lower() for n in names):
                    out.append(Problem("id-has-area-name", f"row:{r['id']}"))
    for a in _areas(env):
        for why in validate_area(a, a["id"], [n for n in names if n != a.get("name")]):
            out.append(Problem("bad-area", f"area:{a['id']}", why))
    for v in _views(env):
        try:
            if load_view(_views_dir(env) / f"{v['id']}.toml")["kind"] not in kinds:
                out.append(Problem("bad-view", f"view:{v['id']}", "its kind is not in the table"))
        except ItemError as e:
            out.append(Problem("bad-view", f"view:{v['id']}", e.message))
    return out


def registry_gaps(kinds: dict[str, Kind] | None = None, guide: str = "") -> dict[str, builtins.list[str]]:
    """Every place that must spell a kind, and the kinds that place lacks. The places: the link table (`LINK_SPEC`), the store names
    (`docs/reference/stores.md` plus the messages database), and the kinds table in the guide (`guide` is its text).
    An empty result means none is missing. The test runs this against the real table and a planted one."""
    kinds = KINDS if kinds is None else kinds
    spelled = set(re.findall(r"^\| `([a-z][a-z-]*)` \|", guide, re.MULTILINE))
    gaps = {
        "LINK_SPEC": [n for n in kinds if n not in LINK_SPEC],
        "guide table": [n for n in kinds if n not in spelled],
    }
    return {where: miss for where, miss in gaps.items() if miss}


# --- the commitment ledger and its extractor eval ------------------------------------------------------------------------

_COMMIT = (
    re.compile(r"^\s*[-*] \[ \] (.+?)\s*$"),  # an open checkbox
    re.compile(r"\b(?:TODO|FIXME|follow[- ]up|next step)s?\s*[:-]\s*(.+?)\s*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:[-*]\s+)?(?:we |I |you |it )?(?:must|need to|needs to|have to|will need to)\s+(.+?)\s*$", re.IGNORECASE
    ),
)


def extract_commitments(text: str) -> builtins.list[str]:
    """What a text promises to do: open checkboxes, `TODO:` and `follow-up:` lines, and lines that say something must be
    done. Plain rules, so a miss is something a reader can see and fix; `recall` measures how many a seeded set finds."""
    found = []
    for line in text.splitlines():
        for pat in _COMMIT:
            m = pat.search(line)
            if m:
                found.append(re.sub(r"\s+", " ", m.group(m.lastindex or 1)).strip(" .").lower())
                break
    return found


def recall(seeded: Iterable[tuple[str, Iterable[str]]]) -> dict:
    """The extractor eval: each case is `(text, the commitments planted in it)`. Reports recall over the planted ones and
    the extras the extractor found that were not planted (a count, not a verdict: an extra may be a true find)."""
    want = hit = extra = 0
    missed = []
    for text, planted in seeded:
        got = set(extract_commitments(text))
        for item in planted:
            want += 1
            norm = re.sub(r"\s+", " ", item).strip(" .").lower()
            if norm in got:
                hit += 1
            else:
                missed.append(item)
        extra += len(got - {re.sub(r"\s+", " ", i).strip(" .").lower() for i in planted})
    return {"planted": want, "found": hit, "recall": (hit / want) if want else 1.0, "missed": missed, "extra": extra}


def ledger_rows(text: str, source: str) -> builtins.list[dict]:
    """The rows an `items.jsonl` ledger would hold for a text: one per commitment, its id a hash of source and words.
    ws does not write the file yet (its page in docs/reference/stores.md comes with that); this is the shape."""
    return [
        {"id": "h" + hashlib.sha256(f"{source}\0{c}".encode()).hexdigest()[:12], "source": source, "text": c}
        for c in extract_commitments(text)
    ]


# --- a small entry point: `python -m ws.items check|get|list|rebuild` -------------------------------------------------------


def main(argv: builtins.list[str] | None = None) -> int:
    """Exit 0 fine, 1 the check found faults (or a get found nothing), 2 usage."""
    a = _LIST(sys.argv[1:] if argv is None else argv)
    try:
        if a[:1] == ["check"]:
            problems = check()
            for p in problems:
                print(p)
            return 1 if problems else 0
        if a[:1] == ["get"] and len(a) == 2:
            row = get(a[1])
            print(json.dumps(row, indent=2, default=str) if row else "no such item")
            return 0 if row else 1
        if a[:1] == ["list"] and len(a) >= 2:
            where = dict(x.split("=", 1) for x in a[2:] if "=" in x)
            for r in list(a[1], where):
                print(json.dumps(r, default=str))
            return 0
        if a[:1] == ["rebuild"]:
            print(rebuild_index())
            return 0
    except WsError as e:
        print(f"{e.kind}: {e.message}", file=sys.stderr)
        return 1
    print("usage: python -m ws.items check | get KIND:ID | list KIND [FIELD=VALUE ...] | rebuild", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
