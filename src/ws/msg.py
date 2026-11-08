"""`ws msg`: leave a message for another session, on any runner or provider, and read what was left for you.

One SQLite database (stdlib `sqlite3`, WAL) at `<state>/msg/messages.db` holds every session's messages, schema
`ws.message/1`: `messages` (who, to whom, when, kind, the body of a non-walled message with its sha256, the idempotency
key, tier, thread, reply_to, hop, latency stamps), `recipients` (read and ack times, apart from the message) and
`envelope` (one body-free row per delivery: seq, from, to, message_id, kind, time sent, time delivered, class, size
bucket S, M or L). The viewer and `ws msg watch` read the envelope table and nothing else. docs/reference/messages.md has
the columns, the rules and the errors; docs/decisions/0023 has the reasons.

Who sends. The sender is never an argument. `send` asks `session.resolve_caller` which session this process is under (the
registry entry whose runner process is an ancestor of this one) and refuses when there is none: a flag, an environment
variable, an MCP argument or a line in the body cannot name a sender. The receiver's data class is looked up from the
policy by the receiver's folder, at send and again at read; nothing a sender passes is ever read as a class. A policy or
secrets registry that will not load refuses the send.

Walled classes. A message to a walled or counts-only session keeps its body only in a store inside that session's repo,
`<repo root>/.ws/messages/<class>.db`, created with a `.gitignore` that ignores everything in the folder; the shared
database gets the envelope, with no body, no hash of a body and no free-text field. A walled or counts-only session sends
fixed kinds only (`ack`, `done`, `stop-and-hand-over:done`, `ask:pending`, `counts`), no body, no key, no thread name.
What may come in is the receiving class's `inbound` setting (off, lead-only, or the sessions it names), for the kinds
`note`, `ask` and `stop-and-hand-over`; there is no rule that lets a reply in, and no owned-task exception. With no
`[classes]` in the policy none of this applies and any registered session may send to any. Deleting a walled repo
deletes its bodies with it; `prune` then drops the envelope rows whose repo folder is gone.

What it caps. A body is at most `BODY_MAX_BYTES`; a sender may send `RATE_PER_MINUTE` messages in one fixed minute; a
reply chain is at most `HOP_MAX` deep; the store stops growing at `STORE_MAX_BYTES`; a body naming a registered secret or
matching a secret rule is refused. The retention window is the receiving class's `retention_days`, pruned at most hourly.

Failures. A store that is locked, full, read-only, unopenable or damaged is a `WsError` of kind `unavailable`, classified by
SQLite's error code and never by its text, with nothing half-written: the body and the envelope are one transaction.
Inside the wall the state folder is read-only, so a send there reports that and says delivery across the wall is not built.

Public API (for the delivery guard and the viewer): `send`, `inbox`, `sent`, `read`, `ack`, `mark_delivered`, `envelopes`,
`export`, `prune`. Every time is UTC. A `now` argument is the clock (tests inject one).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import quote

from ws import globals as repo_globals
from ws import jsonl, output, paths, policy, secrets, session
from ws.output import WsError

COMMAND = "msg"
SCHEMA = "ws.message/1"
STORE_VERSION = 1  # PRAGMA user_version of both databases

KINDS = ("note", "ask", "stop-and-hand-over", "ack", "done", "stop-and-hand-over:done", "ask:pending", "counts")
FIXED_KINDS = ("ack", "done", "stop-and-hand-over:done", "ask:pending", "counts")  # all a walled sender may send
INBOUND_WALLED_KINDS = ("note", "ask", "stop-and-hand-over")  # all a walled receiver may be sent
TIERS = ("info", "urgent", "noise")

BODY_MAX_BYTES = 16 * 1024
RATE_PER_MINUTE = 30
RATE_WINDOW_S = 60  # one fixed window, from the store's own clock
HOP_MAX = 8
STORE_MAX_BYTES = 256 * 1024 * 1024
BUSY_MS = 5000
PRUNE_EVERY_S = 3600
LIST_DEFAULT, LIST_MAX = 50, 200
EXPORT_DEFAULT, EXPORT_MAX = 10_000, 100_000
DEFAULT_RETENTION_DAYS = 30
_BUCKETS = ((1024, "S"), (4096, "M"))  # up to this many bytes; anything over is L

_OPEN_MODE: str | None = None  # None = normal; a test sets "ro" to meet a read-only store
_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")  # an idempotency key or a thread name: a token, not prose
_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CLASS_FILE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # what a terminal would act on

# SQLite primary result codes (stable, documented), by what they mean for the person.
_REASONS = {
    3: "unopenable",  # PERM
    5: "locked",  # BUSY
    6: "locked",  # LOCKED
    8: "read-only",  # READONLY
    10: "unreadable",  # IOERR
    11: "damaged",  # CORRUPT
    13: "full",  # FULL
    14: "unopenable",  # CANTOPEN
    23: "unopenable",  # AUTH
    26: "damaged",  # NOTADB
}
_HINTS = {
    "locked": "another ws process is using the message store; try again in a moment",
    "full": "the message store is at its size cap; export it with `ws msg export`, then remove old messages or the file",
    "read-only": (
        "ws can't write the message store from here (inside the wall the state folder is read-only); send from outside "
        "the wall: delivery across it is not built yet"
    ),
    "unopenable": "check that the store folder exists and is yours, and that its permissions allow you in",
    "unreadable": "the disk gave an error reading or writing the message store; check it and try again",
    "damaged": "the store file is damaged; export what you can with `ws msg export`, then move the file aside",
    "failed": "the message store could not be used; run `ws doctor`",
}
_ERRNO_REASONS = {errno.EROFS: "read-only", errno.ENOSPC: "full", errno.EDQUOT: "full", errno.ENOENT: "unopenable"}

# --- secret scan --------------------------------------------------------------------------------------------------
# A rule is (what it found, lower-case words one of which must be in the text, the pattern). The words are a cheap first
# pass; every quantifier is bounded and no group repeats another, so a hostile body cannot make a rule run long. They
# name what was found, never the text that matched.
_RULES: tuple[tuple[str, tuple[str, ...], re.Pattern], ...] = tuple(
    (name, words, re.compile(pattern))
    for name, words, pattern in (
        ("an AWS access key id", ("akia", "asia", "abia", "acca"), r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        (
            "a GitHub token",
            ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
            r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{50,255})",
        ),
        ("a GitLab token", ("glpat-",), r"\bglpat-[A-Za-z0-9_-]{20,64}"),
        ("an Anthropic key", ("sk-ant-",), r"\bsk-ant-[A-Za-z0-9_-]{20,200}"),
        ("an OpenAI-style key", ("sk-",), r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,200}"),
        ("a Slack token", ("xox", "xapp-"), r"\b(?:xox[abprso]-[A-Za-z0-9-]{10,100}|xapp-[0-9]-[A-Za-z0-9-]{10,100})"),
        ("a Google API key", ("aiza",), r"\bAIza[0-9A-Za-z_-]{35}"),
        ("a Stripe key", ("_live_",), r"\b[sr]k_live_[0-9A-Za-z]{20,99}"),
        ("an npm token", ("npm_",), r"\bnpm_[A-Za-z0-9]{36}"),
        ("a Hugging Face token", ("hf_",), r"\bhf_[A-Za-z0-9]{30,100}"),
        ("a SendGrid key", ("sg.",), r"\bSG\.[A-Za-z0-9_-]{16,32}\.[A-Za-z0-9_-]{16,64}"),
        ("a private key block", ("-----begin",), r"-----BEGIN (?:[A-Z]{2,10} ){0,3}PRIVATE KEY(?: BLOCK)?-----"),
        (
            "a JSON web token",
            ("eyj",),
            r"\beyJ[A-Za-z0-9_-]{8,500}\.eyJ[A-Za-z0-9_-]{8,500}\.[A-Za-z0-9_-]{8,500}",
        ),
        ("a bearer token", ("bearer",), r"(?i)\bbearer[ \t]{1,5}[A-Za-z0-9._~+/=-]{20,500}"),
        ("a password in a web address", ("://",), r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^/\s:@]{1,100}:[^/\s@]{1,100}@"),
        (
            "a key or password with its value",
            ("key", "secret", "token", "passw", "pwd"),
            r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|pwd)\b[ \t]{0,3}[:=][ \t]{0,3}[\"']?[A-Za-z0-9/+_.=-]{12,200}",
        ),
    )
)


def scan(text: str) -> str | None:
    """What the first secret rule that matches `text` found (for example "a GitHub token"), or None. Never the match."""
    low = text.lower()
    for name, words, pattern in _RULES:
        if any(w in low for w in words) and pattern.search(text):
            return name
    return None


def _names_in(text: str, names: Iterable[str]) -> str | None:
    """A registered secret name that appears in `text` as a word of its own, or None."""
    for name in names:
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text):
            return name
    return None


# --- the stores: paths, opening, typed errors -----------------------------------------------------------------------


def store_path(env: dict | None = None) -> Path:
    """The shared database: `<state>/msg/messages.db`."""
    return paths.state_dir(_env(env)) / "msg" / "messages.db"


def _unavailable(reason: str) -> WsError:
    return WsError(
        "unavailable",
        f"the message store is {reason}" if reason != "failed" else "the message store could not be used",
        _HINTS[reason],
        1,
        {"reason": reason},
    )


@contextlib.contextmanager
def _typed() -> Iterator[None]:
    """Turn what a store can throw into a `WsError` of kind `unavailable`, by SQLite's error code or the OS error number.
    An SQLite error with no code is a bug in this module and passes through."""
    try:
        yield
    except WsError:
        raise
    except sqlite3.Error as e:
        code = getattr(e, "sqlite_errorcode", None)
        if not isinstance(code, int):
            raise
        raise _unavailable(_REASONS.get(code & 0xFF, "failed")) from e
    except OSError as e:
        raise _unavailable(_ERRNO_REASONS.get(e.errno or 0, "unopenable")) from e


def _env(env: dict | None) -> dict:
    return paths.merged_environ() if env is None else env


def _private_folder(folder: Path) -> None:
    """The folder, made if need be, private to us. A link where it should be is refused."""
    if folder.is_symlink():
        raise _unavailable("unopenable")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.stat().st_mode & 0o077:
        with contextlib.suppress(OSError):  # a folder that is not ours is left as it is; the files in it are private
            folder.chmod(0o700)


def _private_file(path: Path) -> None:
    """Make sure the file exists, owned by us and private; refuse a link where the file should be."""
    if path.is_symlink():
        raise _unavailable("unopenable")
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))


def _uri(path: Path, mode: str) -> str:
    return f"file:{quote(str(path))}?mode={_OPEN_MODE or mode}"


_SHARED_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        "from" TEXT NOT NULL,
        "to" TEXT NOT NULL,
        sent TEXT NOT NULL,
        sent_ms INTEGER NOT NULL,
        t_signal_ms INTEGER NOT NULL,
        t_stored_ms INTEGER NOT NULL,
        kind TEXT NOT NULL,
        tier TEXT NOT NULL,
        body TEXT,
        sha256 TEXT,
        size_bytes INTEGER,
        idem TEXT,
        thread_id TEXT NOT NULL,
        reply_to INTEGER,
        hop INTEGER NOT NULL,
        class TEXT,
        walled INTEGER NOT NULL DEFAULT 0,
        store TEXT,
        has_body INTEGER NOT NULL DEFAULT 0)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS messages_idem ON messages("from", idem) WHERE idem IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS messages_rate ON messages("from", sent_ms)""",
    """CREATE INDEX IF NOT EXISTS messages_to ON messages("to", id)""",
    """CREATE TABLE IF NOT EXISTS recipients(
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        recipient TEXT NOT NULL,
        read_ts TEXT,
        ack_ts TEXT,
        ack_required INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(message_id, recipient)) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS recipients_by_who ON recipients(recipient, message_id)""",
    """CREATE TABLE IF NOT EXISTS envelope(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        "from" TEXT NOT NULL,
        "to" TEXT NOT NULL,
        kind TEXT NOT NULL,
        sent TEXT NOT NULL,
        delivered_ts TEXT,
        class TEXT,
        size TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS envelope_by_message ON envelope(message_id)""",
)
_CLASS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS {db}bodies(
        message_id INTEGER PRIMARY KEY,
        "to" TEXT NOT NULL,
        sent_ms INTEGER NOT NULL,
        body TEXT NOT NULL)""",
)


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(path, mode), uri=True, timeout=BUSY_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _limit_size(conn: sqlite3.Connection, schema: str = "main") -> None:
    page = conn.execute(f"PRAGMA {schema}.page_size").fetchone()[0]
    conn.execute(f"PRAGMA {schema}.max_page_count={max(STORE_MAX_BYTES // page, 64)}")


def _open(env: dict | None, *, create: bool) -> sqlite3.Connection | None:
    """The shared database, ready to use. With `create` false a store that does not exist is None (nothing sent yet)."""
    path = store_path(env)
    if create:
        _private_folder(path.parent)
        _private_file(path)
    elif not path.exists():
        return None
    elif path.is_symlink():
        raise _unavailable("unopenable")
    conn = _connect(path, "rwc" if create else "rw")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal" and (_OPEN_MODE or "rw") != "ro":
            conn.execute("PRAGMA journal_mode=WAL")
        _limit_size(conn)
        _ensure_schema(conn, _SHARED_SCHEMA)
    except BaseException:
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection, statements: tuple[str, ...], db: str = "") -> None:
    pragma = f"PRAGMA {db}user_version"
    if conn.execute(pragma).fetchone()[0] >= STORE_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(pragma).fetchone()[0] < STORE_VERSION:
            for stmt in statements:
                conn.execute(stmt.format(db=f"{db.rstrip('.')}." if db else ""))
            conn.execute(f"{pragma}={STORE_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _class_file(name: str) -> str:
    return name if _CLASS_FILE.fullmatch(name) else hashlib.sha256(name.encode()).hexdigest()[:32]


def _repo_root(cwd: str) -> Path:
    """The repo a session's folder is in: the nearest folder above it (or it) holding `.git`; else the folder itself."""
    here = Path(os.path.realpath(cwd))
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return here


def _class_db(root: str | Path, class_name: str) -> Path:
    """Where the bodies of class `class_name` are kept for the repo at `root`: `<root>/<repo ws folder>/messages/<class>.db`."""
    return Path(root) / repo_globals.OVERRIDES.parent / "messages" / f"{_class_file(class_name)}.db"


def _class_store(pol: policy.Policy, cls: policy.Class, cwd: str) -> tuple[Path, Path]:
    """(repo root, database path) for the bodies of class `cls` at a receiver working in `cwd`. Refused when the root is
    not inside what that class's `repos` cover, or when the `.ws/messages` path is a link (a repo could point it out of
    the wall)."""
    root = _repo_root(cwd)
    if not root.is_dir():
        raise WsError("not_found", "the receiving session's folder is gone", "it has ended; message another session")
    found = policy.class_of(pol, root)
    if found is None or found.name != cls.name:
        raise WsError(
            "refused",
            f"the repo of the receiving session is not inside the paths that class {cls.name!r} covers",
            "add the repo to the class's `repos`, or message a session in a covered repo",
        )
    db = _class_db(root, cls.name)
    if db.parent.parent.is_symlink() or db.parent.is_symlink():
        raise WsError("refused", "the ws folder of the receiving repo, or its messages folder, is a link", "remove it")
    return root, db


def _prepare_class_folder(db: Path) -> None:
    _private_folder(db.parent)
    ignore = db.parent / ".gitignore"  # ignores everything in the folder, itself too, so `git add` cannot take a body
    if not ignore.exists():
        with contextlib.suppress(FileExistsError):
            fd = os.open(ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("*\n")
    _private_file(db)


def _attach_class(conn: sqlite3.Connection, db: Path) -> None:
    _prepare_class_folder(db)
    conn.execute("ATTACH DATABASE ? AS cls", (_uri(db, "rwc"),))
    _limit_size(conn, "cls")
    _ensure_schema(conn, _CLASS_SCHEMA, "cls.")


def _class_connect(db: Path) -> sqlite3.Connection | None:
    """A class store opened to read or tidy, or None when it is not there."""
    if not db.is_file() or db.is_symlink():
        return None
    return _connect(db, "rw")


# --- small helpers --------------------------------------------------------------------------------------------------


def _t(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _bucket(size: int) -> str:
    return next((b for limit, b in _BUCKETS if size <= limit), "L")


def _walled(cls: policy.Class | None) -> bool:
    return cls is not None and cls.access != "full"


def _bad(message: str, hint: str = "") -> WsError:
    return WsError("bad_input", message, hint, 2)


def caller(env: dict | None = None) -> session.Record:
    """The session this process is under (`session.resolve_caller`), or a `refused` error when there is none."""
    rec = session.resolve_caller(env)
    if rec is None:
        raise WsError(
            "refused",
            "ws can't tell which session is calling",
            "run it from inside a session (or through its `ws mcp serve`); a person's shell has no send path yet",
        )
    return rec


def _policy(env: dict | None) -> policy.Policy:
    try:
        return policy.load(policy.policy_path(env))
    except (policy.PolicyError, OSError, ValueError) as e:
        raise WsError(
            "refused",
            "ws can't read its policy, so it can't tell which data class each session is in",
            "fix policy.toml (`ws doctor` says where)",
        ) from e


def _secret_names(env: dict | None) -> list[str]:
    try:
        return sorted(secrets.load_registry(secrets.registry_path(env)))
    except (secrets.SecretsError, OSError) as e:
        raise WsError(
            "refused", "ws can't read the secrets registry, so it can't check the message for secret names", "fix it"
        ) from e


def _retention_days(pol: policy.Policy, name: str | None) -> int:
    cls = pol.classes.get(name) if name else None
    return cls.retention_days if cls else DEFAULT_RETENTION_DAYS


def _int(value, what: str, low: int = 1, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        span = f"a whole number from {low} to {high}" if high else f"a whole number of {low} or more"
        raise _bad(f"{what} must be {span}")
    return value


def _label(value, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise _bad(f"{what} must be 1 to 64 letters, digits, `.`, `_`, `:` or `-`")
    return value


# --- send -----------------------------------------------------------------------------------------------------------


def _admit(
    sender: session.Record,
    s_class: policy.Class | None,
    r_class: policy.Class | None,
    kind: str,
    body: str | None,
    key: str | None,
    thread: str | None,
) -> None:
    """The rules about who may send what to whom. Raises `WsError` (refused) or returns."""
    if _walled(s_class):
        if kind not in FIXED_KINDS:
            raise WsError(
                "refused",
                f"a session in a walled class sends only {', '.join(FIXED_KINDS)}; {kind!r} is free text",
                "send one of those kinds, with no text",
            )
        if body:
            raise WsError("refused", "a session in a walled class can't send a message body", "send the kind alone")
        if key is not None or thread is not None:
            raise WsError(
                "refused", "a session in a walled class can't set an idempotency key or a thread name", "leave them out"
            )
    if r_class is None:
        return  # no [classes]: any registered session may send to any
    if r_class.inbound == "off":
        raise WsError(
            "refused",
            f"sessions in class {r_class.name!r} take no messages",
            "the class's `inbound` in policy.toml is off; set lead-only or allowlist if that is wanted",
        )
    if r_class.inbound == "lead-only" and sender.role != "lead":
        raise WsError("refused", f"class {r_class.name!r} takes messages from the lead only", "ask the lead to send it")
    if r_class.inbound == "allowlist" and sender.session_id not in r_class.inbound_allow:
        raise WsError("refused", f"class {r_class.name!r} takes messages from named sessions only", "this is not one")
    if _walled(r_class):
        if kind not in INBOUND_WALLED_KINDS:
            raise WsError(
                "refused",
                f"a session in a walled class takes only {', '.join(INBOUND_WALLED_KINDS)}, not {kind!r}",
                "send one of those",
            )
        if thread is not None:
            raise WsError("refused", "a thread name can't be set on a message to a walled class", "leave it out")


def send(
    to: str,
    kind: str = "note",
    body: str | None = None,
    *,
    key: str | None = None,
    thread: str | None = None,
    reply_to: int | None = None,
    tier: str = "info",
    ack_required: bool = False,
    env: dict | None = None,
    now: float | None = None,
) -> dict:
    """Leave `body` for session `to`. The sender is the session this process is under (`session.resolve_caller`); there
    is no way to pass one. Returns the stored message's `id`, `thread_id`, `hop`, `class`, `size` bucket and `replayed`
    (True when the same sender's `key` was used before: the first message's id comes back and nothing is stored)."""
    env = _env(env)
    signal = _t(now)
    if not isinstance(to, str) or not _SESSION_ID.fullmatch(to):
        raise _bad("the recipient must be a session id")
    if kind not in KINDS:
        raise _bad(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if tier not in TIERS:
        raise _bad(f"tier must be one of {', '.join(TIERS)}")
    if body is not None and not isinstance(body, str):
        raise _bad("the body must be text")
    if not isinstance(ack_required, bool):
        raise _bad("ack_required must be true or false")
    key, thread = _label(key, "the idempotency key"), _label(thread, "the thread name")
    if reply_to is not None:
        reply_to = _int(reply_to, "reply_to")
    data = (body or "").encode("utf-8", "replace")
    if len(data) > BODY_MAX_BYTES:
        raise WsError(
            "too_large", f"the body is {len(data)} bytes; the most is {BODY_MAX_BYTES}", "send a shorter message", 1
        )

    sender = caller(env)
    pol = _policy(env)
    names = _secret_names(env)
    target = session.read(to, env)
    if target is None:
        raise WsError("not_found", f"no session {to!r}", "the registry has no session with that id")
    s_class = policy.class_of(pol, sender.cwd)
    r_class = policy.class_of(pol, target.cwd)
    _admit(sender, s_class, r_class, kind, body, key, thread)
    if kind in ("note", "ask") and not data:
        raise _bad(f"a {kind} needs some text")

    text = body or ""
    for field in (text, thread or ""):
        if (found := _names_in(field, names)) is not None:
            raise WsError(
                "secret_refused",
                f"the message names the registered secret {found!r}",
                "say what the secret is for, not its name; the value never goes in a message",
                1,
            )
        if (what := scan(field)) is not None:
            raise WsError(
                "secret_refused", f"the message looks like it holds {what}", "take it out; a message is not a vault", 1
            )

    # a walled receiver's body goes to its repo's own store, never the shared one; the repo is recorded either way, so
    # the rows can be dropped when the repo is deleted
    store = _class_store(pol, r_class, target.cwd) if r_class is not None and _walled(r_class) else None
    idem = hashlib.sha256(f"{sender.session_id}\0{key}".encode()).hexdigest() if key else None

    with _typed():
        conn = _open(env, create=True)
        if conn is None:  # create=True never answers None; this keeps the type honest
            raise _unavailable("unopenable")
        try:
            _housekeeping(conn, env, now, pol)
            if store and data:
                _attach_class(conn, store[1])
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = _store_message(
                    conn,
                    (sender, target, r_class, store),
                    (kind, tier, text, data, ack_required),
                    (idem, thread, reply_to),
                    (signal, now),
                )
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    return out


def _store_message(conn, parties, content, labels, clock) -> dict:
    """The writes of one send, inside its transaction: the replay check, the rate and hop caps, the message, its
    recipient row, its envelope row and (for a walled receiver) its body in the class store. All of it or none."""
    sender, target, r_class, store = parties
    kind, tier, text, data, ack_required = content
    idem, thread, reply_to = labels
    signal, now = clock
    sid = sender.session_id
    if idem:
        first = conn.execute(
            'SELECT id, thread_id, hop, class FROM messages WHERE "from"=? AND idem=?', (sid, idem)
        ).fetchone()
        if first:
            return {
                "id": first["id"],
                "thread_id": first["thread_id"],
                "hop": first["hop"],
                "class": first["class"],
                "size": None,
                "replayed": True,
            }
    stored = _t(now)  # the store's own clock: the stamp, the rate window and the retention all read this one
    stored_ms = int(stored * 1000)
    window = stored - (stored % RATE_WINDOW_S)
    (recent,) = conn.execute(
        'SELECT COUNT(*) FROM messages WHERE "from"=? AND sent_ms>=?', (sid, int(window * 1000))
    ).fetchone()
    if recent >= RATE_PER_MINUTE:
        raise WsError(
            "rate_limited", f"{sid!r} has sent {RATE_PER_MINUTE} messages this minute", "wait for the next minute", 1
        )
    hop, thread_id = 0, thread
    if reply_to is not None:
        parent = conn.execute(
            'SELECT hop, thread_id FROM messages WHERE id=? AND ("from"=? OR "to"=?)', (reply_to, sid, sid)
        ).fetchone()
        if parent is None:
            raise WsError(
                "not_found", f"no message {reply_to} that you sent or were sent", "reply_to is an id from `ws msg list`"
            )
        if thread is not None and thread != parent["thread_id"]:
            raise _bad("a reply stays in its thread; leave the thread name out")
        hop, thread_id = parent["hop"] + 1, parent["thread_id"]
        if hop > HOP_MAX:
            raise WsError(
                "hop_limit", f"that reply would be {hop} deep; the most is {HOP_MAX}", "start a new thread", 1
            )
    walled_body = store is not None
    cur = conn.execute(
        'INSERT INTO messages("from","to",sent,sent_ms,t_signal_ms,t_stored_ms,kind,tier,body,sha256,size_bytes,idem,'
        "thread_id,reply_to,hop,class,walled,store,has_body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sid,
            target.session_id,
            jsonl.iso_utc(stored),
            stored_ms,
            int(signal * 1000),
            stored_ms,
            kind,
            tier,
            None if walled_body or not data else text,
            None if walled_body or not data else hashlib.sha256(data).hexdigest(),
            None if walled_body else len(data),
            idem,
            thread_id or "",
            reply_to,
            hop,
            r_class.name if r_class else None,
            1 if walled_body else 0,
            str(store[0]) if store else None,
            1 if data else 0,
        ),
    )
    mid = cur.lastrowid
    if thread_id is None:
        thread_id = str(mid)
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (thread_id, mid))
    conn.execute(
        "INSERT INTO recipients(message_id,recipient,ack_required) VALUES(?,?,?)",
        (mid, target.session_id, 1 if ack_required else 0),
    )
    if walled_body and data:  # the body goes in before the envelope, so a failure on either side leaves neither
        conn.execute(
            'INSERT OR REPLACE INTO cls.bodies(message_id,"to",sent_ms,body) VALUES(?,?,?,?)',
            (mid, target.session_id, stored_ms, text),
        )
    bucket = _bucket(len(data))
    conn.execute(
        'INSERT INTO envelope(message_id,"from","to",kind,sent,class,size) VALUES(?,?,?,?,?,?,?)',
        (mid, sid, target.session_id, kind, jsonl.iso_utc(stored), r_class.name if r_class else None, bucket),
    )
    return {
        "id": mid,
        "thread_id": thread_id,
        "hop": hop,
        "class": r_class.name if r_class else None,
        "size": bucket,
        "replayed": False,
    }


# --- read -----------------------------------------------------------------------------------------------------------

_LIST_SQL = """
SELECT m.id, m."from" AS sender, m."to" AS recipient, m.sent, m.kind, m.tier, m.thread_id, m.reply_to, m.hop, m.class,
       m.walled, m.store, m.body, m.sha256, m.has_body, m.size_bytes, r.read_ts, r.ack_ts, r.ack_required,
       e.delivered_ts, e.size AS size
FROM messages m
JOIN recipients r ON r.message_id = m.id
LEFT JOIN envelope e ON e.message_id = m.id AND e."to" = r.recipient
"""


def _public(row: sqlite3.Row) -> dict:
    """A message row as the person or agent sees it: the envelope, never a body, never where a body is kept."""
    return {
        "id": row["id"],
        "from": row["sender"],
        "to": row["recipient"],
        "sent": row["sent"],
        "kind": row["kind"],
        "tier": row["tier"],
        "thread_id": row["thread_id"],
        "reply_to": row["reply_to"],
        "hop": row["hop"],
        "class": row["class"],
        "size": row["size"],
        "ack_required": bool(row["ack_required"]),
        "read_ts": row["read_ts"],
        "ack_ts": row["ack_ts"],
        "delivered_ts": row["delivered_ts"],
    }


def _window(limit, after) -> tuple[int, int]:
    return _int(limit, "limit", 1, LIST_MAX), _int(after, "after", 0)


def _rows(env, where: str, args: tuple, limit: int, now: float | None = None) -> list[sqlite3.Row]:
    with _typed():
        conn = _open(env, create=False)
        if conn is None:
            return []
        try:
            _housekeeping(conn, env, now)
            return conn.execute(f"{_LIST_SQL} WHERE {where} ORDER BY m.id LIMIT ?", (*args, limit)).fetchall()
        finally:
            conn.close()


def _session(env, session_id: str) -> session.Record:
    rec = session.read(session_id, env) if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id) else None
    if rec is None:
        raise WsError("refused", f"no readable session record for {session_id!r}", "start the session first")
    return rec


def inbox(
    session_id: str,
    *,
    unread_only: bool = False,
    after: int = 0,
    limit: int = LIST_DEFAULT,
    bodies: bool = False,
    env: dict | None = None,
    now: float | None = None,
) -> list[dict]:
    """The messages left for `session_id`, oldest first, after message id `after`, at most `limit` (to 200). Each is its
    envelope; with `bodies` each also has `body` and `body_state` (the delivery guard asks for these). The id is the
    reader's: a caller that got it from the process tree (`session.resolve_caller`) or from a hook payload."""
    env = _env(env)
    limit, after = _window(limit, after)
    where = "r.recipient=? AND m.id>?" + (" AND r.read_ts IS NULL" if unread_only else "")
    rows = _rows(env, where, (session_id, after), limit, now)
    out = [_public(r) for r in rows]
    if bodies and rows:
        reader, pol = _session(env, session_id), _policy(env)
        for item, row in zip(out, rows, strict=True):
            item["body"], item["body_state"] = _body(row, reader, pol)
    return out


def sent(
    session_id: str,
    *,
    after: int = 0,
    limit: int = LIST_DEFAULT,
    env: dict | None = None,
    now: float | None = None,
) -> list[dict]:
    """The messages `session_id` sent (envelopes only), oldest first."""
    limit, after = _window(limit, after)
    return [_public(r) for r in _rows(_env(env), 'm."from"=? AND m.id>?', (session_id, after), limit, now)]


def _body(row: sqlite3.Row, reader: session.Record, pol: policy.Policy) -> tuple[str | None, str]:
    """(body, state) for a message row the reader is the recipient of. State: `present`, `none` (nothing was sent with it),
    `withheld` (a walled body, and the reader is not in that class) or `gone` (the store holding it is not there)."""
    if not row["has_body"]:
        return None, "none"
    if not row["walled"]:
        return row["body"], "present"
    cls = policy.class_of(pol, reader.cwd)
    if cls is None or cls.name != row["class"] or not row["store"]:
        return None, "withheld"
    db = _class_db(row["store"], cls.name)
    with _typed():
        conn = _class_connect(db)
        if conn is None:
            return None, "gone"
        try:
            got = conn.execute("SELECT body FROM bodies WHERE message_id=?", (row["id"],)).fetchone()
        finally:
            conn.close()
    return (got["body"], "present") if got is not None else (None, "gone")


def read(message_id: int, session_id: str, *, env: dict | None = None, now: float | None = None) -> dict:
    """One message addressed to `session_id`, with its body, marked read. A message that is not addressed to it, or does
    not exist, is `not_found` (the same answer, so ids can't be probed). A walled body comes only to a reader in that class."""
    env = _env(env)
    mid = _int(message_id, "the message id")
    reader, pol = _session(env, session_id), _policy(env)
    stamp = jsonl.iso_utc(_t(now))
    with _typed():
        conn = _open(env, create=False)
        if conn is None:
            raise _missing(mid)
        try:
            row = conn.execute(f"{_LIST_SQL} WHERE m.id=? AND r.recipient=?", (mid, session_id)).fetchone()
            if row is None:
                raise _missing(mid)
            body, state = _body(row, reader, pol)
            conn.execute(
                "UPDATE recipients SET read_ts=COALESCE(read_ts,?) WHERE message_id=? AND recipient=?",
                (stamp, mid, session_id),
            )
            conn.execute(
                'UPDATE envelope SET delivered_ts=COALESCE(delivered_ts,?) WHERE message_id=? AND "to"=?',
                (stamp, mid, session_id),
            )
        finally:
            conn.close()
    item = _public(row)
    item["read_ts"] = item["read_ts"] or stamp
    item["delivered_ts"] = item["delivered_ts"] or stamp
    item["body"], item["body_state"] = body, state
    return item


def _missing(mid: int) -> WsError:
    return WsError("not_found", f"no message {mid} for you", "`ws msg list` shows yours")


def ack(message_id: int, session_id: str, *, env: dict | None = None, now: float | None = None) -> dict:
    """Mark a message addressed to `session_id` acknowledged (and read). The first ack time stays."""
    env = _env(env)
    mid = _int(message_id, "the message id")
    stamp = jsonl.iso_utc(_t(now))
    with _typed():
        conn = _open(env, create=False)
        if conn is None:
            raise _missing(mid)
        try:
            cur = conn.execute(
                "UPDATE recipients SET ack_ts=COALESCE(ack_ts,?), read_ts=COALESCE(read_ts,?) "
                "WHERE message_id=? AND recipient=?",
                (stamp, stamp, mid, session_id),
            )
            if cur.rowcount == 0:
                raise _missing(mid)
            got = conn.execute(
                "SELECT read_ts, ack_ts FROM recipients WHERE message_id=? AND recipient=?", (mid, session_id)
            ).fetchone()
        finally:
            conn.close()
    return {"id": mid, "read_ts": got["read_ts"], "ack_ts": got["ack_ts"]}


def mark_delivered(
    message_ids: int | Iterable[int], to: str, *, env: dict | None = None, now: float | None = None
) -> int:
    """Stamp the envelope of each message to `to` as delivered (the first time only). Returns how many were stamped."""
    ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
    stamp = jsonl.iso_utc(_t(now))
    with _typed():
        conn = _open(_env(env), create=False)
        if conn is None:
            return 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            n = 0
            for mid in ids:
                n += conn.execute(
                    'UPDATE envelope SET delivered_ts=? WHERE message_id=? AND "to"=? AND delivered_ts IS NULL',
                    (stamp, _int(mid, "the message id"), to),
                ).rowcount
            conn.execute("COMMIT")
            return n
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def envelopes(*, after: int = 0, limit: int = LIST_DEFAULT, env: dict | None = None) -> list[dict]:
    """The body-free feed the viewer and `ws msg watch` read: envelope rows after sequence number `after`, oldest first."""
    limit, after = _window(limit, after)
    with _typed():
        conn = _open(_env(env), create=False)
        if conn is None:
            return []
        try:
            rows = conn.execute(
                'SELECT seq, message_id, "from", "to", kind, sent, delivered_ts, class, size FROM envelope '
                "WHERE seq>? ORDER BY seq LIMIT ?",
                (after, limit),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- retention and deletion -----------------------------------------------------------------------------------------


def _housekeeping(conn: sqlite3.Connection, env: dict, now: float | None, pol: policy.Policy | None = None) -> None:
    """Prune when the last prune was an hour ago or more. Tidying is never a reason to fail the call it rides on."""
    try:
        _prune(conn, env, _t(now), pol, force=False)
    except (WsError, sqlite3.Error, OSError):
        return


def _repo_gone(root: str) -> bool:
    """True only when the repo folder is certainly not there. A folder we are not allowed to see is not gone."""
    try:
        os.stat(root)
    except FileNotFoundError:
        return Path(root).parent.is_dir()
    except OSError:
        return False
    return False


def _prune(conn: sqlite3.Connection, env: dict, now: float, pol: policy.Policy | None, *, force: bool) -> dict:
    last = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    if not force and last is not None and now - float(last["value"]) < PRUNE_EVERY_S:
        return {"ran": False, "expired": 0, "gone": 0}
    pol = pol or _policy(env)  # a policy that won't load is a prune that doesn't happen: retention can't be known
    touched: set[tuple[str, str]] = set()  # (repo root, class) of walled rows whose bodies may need tidying
    gone = expired = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute("SELECT DISTINCT store FROM messages WHERE store IS NOT NULL").fetchall():
            if _repo_gone(row["store"]):
                gone += conn.execute("DELETE FROM messages WHERE store=?", (row["store"],)).rowcount
        for row in conn.execute("SELECT DISTINCT class FROM messages").fetchall():
            cutoff = int((now - _retention_days(pol, row["class"]) * 86400) * 1000)
            old = "FROM messages WHERE class IS ? AND sent_ms<?"
            for walled in conn.execute(f"SELECT DISTINCT store {old} AND store IS NOT NULL", (row["class"], cutoff)):
                touched.add((walled["store"], row["class"]))
            expired += conn.execute(f"DELETE {old}", (row["class"], cutoff)).rowcount
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_prune',?)", (repr(now),))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    for root, cls in touched:
        _tidy_class_store(root, cls, pol, now)
    return {"ran": True, "expired": expired, "gone": gone}


def _tidy_class_store(root: str, cls: str, pol: policy.Policy, now: float) -> None:
    """Drop the bodies past the class's retention from the store in `root`, when it can be reached. That covers the
    messages just expired and any body left by a crash between the two stores. A store that can't be reached is left."""
    conn = None
    with contextlib.suppress(sqlite3.Error, OSError):
        conn = _class_connect(_class_db(root, cls))
        if conn is not None:
            conn.execute("DELETE FROM bodies WHERE sent_ms<?", (int((now - _retention_days(pol, cls) * 86400) * 1000),))
    if conn is not None:
        conn.close()


def prune(*, env: dict | None = None, now: float | None = None, force: bool = False) -> dict:
    """Drop messages past their receiving class's `retention_days` (30 with no class), and the rows of walled messages
    whose repo folder is gone (their bodies went with it). Runs at most once an hour unless `force`. Returns `ran`,
    `expired` and `gone`. A policy that will not load refuses (retention can't be known)."""
    env = _env(env)
    with _typed():
        conn = _open(env, create=False)
        if conn is None:
            return {"ran": False, "expired": 0, "gone": 0}
        try:
            return _prune(conn, env, _t(now), None, force=force)
        finally:
            conn.close()


# --- export ---------------------------------------------------------------------------------------------------------


def export(
    out,
    *,
    session_id: str | None = None,
    after: int = 0,
    limit: int = EXPORT_DEFAULT,
    env: dict | None = None,
) -> int:
    """Write messages to `out` (a text stream) as JSON lines, each carrying `schema: "ws.message/1"`, and return how many.
    Never a walled body, for anyone: those rows carry the envelope and `body: null`. With `session_id` only that session's
    messages (sent or received); without, the whole store (a person at their own terminal). Not written through ws.jsonl: it
    is an export, not a log."""
    limit = _int(limit, "limit", 1, EXPORT_MAX)
    after = _int(after, "after", 0)
    where, args = "m.id>?", [after]
    if session_id is not None:
        where, args = 'm.id>? AND (m."from"=? OR r.recipient=?)', [after, session_id, session_id]
    with _typed():
        conn = _open(_env(env), create=False)
        if conn is None:
            return 0
        try:
            rows = conn.execute(f"{_LIST_SQL} WHERE {where} ORDER BY m.id LIMIT ?", (*args, limit)).fetchall()
        finally:
            conn.close()
    for r in rows:
        line = _public(r)
        shared = not r["walled"]  # a walled row has no body here to leave out: its body is not in this database
        line.update(
            {
                "schema": SCHEMA,
                "size_bytes": r["size_bytes"] if shared else None,
                "body": r["body"] if shared else None,
                "body_sha256": r["sha256"] if shared else None,
                "body_kept_in_class_store": bool(r["walled"] and r["has_body"]),
            }
        )
        out.write(json.dumps(line, ensure_ascii=True, separators=(",", ":")) + "\n")
    return len(rows)


# --- commands -------------------------------------------------------------------------------------------------------


def _show(text: str | None) -> str:
    """Text someone else wrote, made safe to print on a terminal: control characters become a replacement mark."""
    return _CONTROL.sub("�", text or "")


def _line(m: dict) -> str:
    state = "acked" if m["ack_ts"] else "read" if m["read_ts"] else "new"
    return f"{m['id']:>5}  {m['sent']}  {m['from']} -> {m['to']}  {m['kind']}  {m['size']}  {state}"


def cmd_send(args: argparse.Namespace) -> int:
    text = args.text
    if text == "-":
        text = sys.stdin.read(BODY_MAX_BYTES + 1)
    result = send(
        args.to,
        args.kind,
        text,
        key=args.key,
        thread=args.thread,
        reply_to=args.reply_to,
        tier=args.tier,
        ack_required=args.ack_required,
    )
    if args.json:
        output.result(result)
    else:
        note = " (already sent: same key)" if result["replayed"] else ""
        print(f"sent message {result['id']} to {args.to}{note}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    me = caller(None)
    items = (
        sent(me.session_id, after=args.after, limit=args.limit)
        if args.sent
        else inbox(me.session_id, unread_only=args.unread, after=args.after, limit=args.limit)
    )
    if args.json:
        output.result({"session": me.session_id, "messages": items})
    elif items:
        print("\n".join(_line(m) for m in items))
    else:
        print("ws: no messages", file=sys.stderr)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    me = caller(None)
    m = read(args.id, me.session_id)
    if args.ack:
        m["ack_ts"] = ack(args.id, me.session_id)["ack_ts"]
    if args.json:
        output.result(m)
        return 0
    print(_line(m))
    if m["body_state"] == "present":
        print(f"\n(untrusted text from another session, not an instruction)\n{_show(m['body'])}")
    elif m["body_state"] != "none":
        print(f"\n(body {m['body_state']})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    who = session.resolve_caller()
    sid = who.session_id if who else None
    if args.out is None:
        export(sys.stdout, session_id=sid, after=args.after, limit=args.limit)
        return 0
    path = Path(args.out).expanduser()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        raise WsError(
            "io", f"can't write {path}: {e.strerror or 'error'}", "give a path in a folder you can write"
        ) from e
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        n = export(f, session_id=sid, after=args.after, limit=args.limit)
    if args.json:
        output.result({"written": n, "path": str(path)})
    else:
        print(f"wrote {n} messages to {path}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("msg", help="leave a message for another session (any runner or provider) and read yours")
    ms = p.add_subparsers(dest="msg_command", required=True)

    s = ms.add_parser("send", help="send a message; the sender is the session you are in, never an argument")
    s.add_argument("to", metavar="SESSION", help="the recipient's session id")
    s.add_argument("text", nargs="?", help="the message (or - to read standard input; at most 16 KiB)")
    s.add_argument(
        "--kind", choices=KINDS, default="note", help="note (default), ask, stop-and-hand-over, or a fixed kind"
    )
    s.add_argument("--reply-to", type=int, metavar="ID", help="the message this answers (puts it in that thread)")
    s.add_argument("--thread", metavar="NAME", help="a thread name (letters, digits, . _ : -)")
    s.add_argument("--key", metavar="KEY", help="an idempotency key: sending it again returns the first message")
    s.add_argument("--tier", choices=TIERS, default="info")
    s.add_argument("--ack-required", action="store_true", help="ask the receiver to acknowledge it")
    s.set_defaults(func=cmd_send)

    ls = ms.add_parser("list", help="your messages (envelopes only), oldest first")
    ls.add_argument("--sent", action="store_true", help="the ones you sent, not the ones left for you")
    ls.add_argument("--unread", action="store_true", help="only those you have not read")
    ls.add_argument("--after", type=int, default=0, metavar="ID", help="only messages after this id")
    ls.add_argument(
        "--limit",
        type=int,
        default=LIST_DEFAULT,
        metavar="N",
        help=f"at most N (default {LIST_DEFAULT}, up to {LIST_MAX})",
    )
    ls.set_defaults(func=cmd_list)

    r = ms.add_parser("read", help="read one message left for you (the body is shown only to the recipient)")
    r.add_argument("id", type=int)
    r.add_argument("--ack", action="store_true", help="acknowledge it too")
    r.set_defaults(func=cmd_read)

    e = ms.add_parser("export", help="write messages as JSON lines (never a walled body)")
    e.add_argument("--out", metavar="PATH", help="a file to write (default: standard output)")
    e.add_argument("--after", type=int, default=0, metavar="ID", help="only messages after this id")
    e.add_argument(
        "--limit",
        type=int,
        default=EXPORT_DEFAULT,
        metavar="N",
        help=f"at most N (default {EXPORT_DEFAULT}, up to {EXPORT_MAX})",
    )
    e.set_defaults(
        func=cmd_export, raw=lambda a: a.out is None
    )  # lines on stdout are the output, so no envelope around them
