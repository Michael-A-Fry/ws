"""OpenCode's own session state, read from its SQLite store and nothing else: id, title, folder, times, tokens and cost.

Checked against OpenCode 1.19.4. That release keeps every session of every project in one SQLite file,
`opencode.db` in `$XDG_DATA_HOME/opencode` (`~/.local/share/opencode` by default; `OPENCODE_DB` moves or renames it, and
`[home.sources] opencode_dir` names the folder). Why the file and not the program: `opencode session list --format json`
lists only the sessions of the project the program starts in, and gives no tokens, cost or state, so it can't be the
one list of every folder. The pending-permission list is held in the running server's memory (its `/permission` route);
the store has no row for one, and a tool call waiting for a yes looks the same on disk as one still running. So this
source gives sessions and no asks, and states nothing it can't see.

The store is opened read-only (`query_only`), so ws can't write to it, and reading it leaves no file behind: a
WAL-mode store with no `-wal` file beside it (no writer is live) is opened `immutable=1`, which makes no side files; one
with a `-wal` file (OpenCode is running) is opened `mode=ro`, so the writer's latest rows are seen. If that folder can't
be written to and holds no `-shm`, SQLite may be unable to open the store, and that is one unreadable row. A session's tokens and cost come from
the `session` row's own totals, and its state from two fields of its latest `message` row (its role and when it
completed). The `part` table, where the text of a conversation is kept, is never queried, and neither is any other
field of a message. A session in an opaque folder (or one with no folder ws can place) is listed by id, folder and
times only: its title, tokens and cost aren't read. Only sessions with no parent that were updated in the last day and
aren't archived are listed, newest first, at most MAX_SESSIONS. A store that is missing, is not a SQLite file, is
locked, or lacks a column this source reads is one `sources[]` row with a typed reason; no session is used from it.
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from ws.home import records
from ws.home.records import Session
from ws.home.sources import BAD_FORMAT, FAILED, TIMEOUT, UNAVAILABLE, UNREADABLE, SourceError, _files

NAME = "opencode"
PROVIDES = ("sessions",)
ORDER = 20
RUNNER = "opencode"
DB_NAME = "opencode.db"
WINDOW_S = 24 * 3600  # a session not updated within this is not listed
WORKING_S = 600  # a reply still being written counts as working while the session changed within this
MAX_SESSIONS = 200
BUSY_MS = 2000  # how long a read waits on a locked store

# the columns this source reads; a store without one is a layout ws doesn't know
_SESSION = ("id", "directory", "title", "parent_id", "time_created", "time_updated", "time_archived", "cost",
            "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write")  # fmt: skip
_MESSAGE = ("id", "session_id", "data", "time_created")

# SQLite's primary result codes (the low byte of `sqlite_errorcode`)
_ERROR, _PERM, _BUSY, _LOCKED, _CORRUPT, _CANTOPEN, _NOTADB = 1, 3, 5, 6, 11, 14, 26


def data_dir(environ, home: Path) -> Path:
    """OpenCode's data folder: `$XDG_DATA_HOME/opencode`, else `.local/share/opencode` under the home folder, the same
    on every system."""
    base = environ.get("XDG_DATA_HOME")
    if base and os.path.isabs(base):
        return Path(base) / "opencode"
    return home / ".local" / "share" / "opencode"


def db_path(env) -> Path:
    """Where the store is: the profile's folder, else `OPENCODE_DB` (a path, or a name inside the data folder), else
    `opencode.db` in the data folder."""
    given = env.environ.get("OPENCODE_DB")
    folder = env.path("opencode_dir")
    if "opencode_dir" in env.cfg.sources:
        return folder / DB_NAME
    if given and given != ":memory:":
        return Path(given) if os.path.isabs(given) else folder / given
    return folder / DB_NAME


def available(env):
    path = db_path(env)
    if path.name.endswith(".key"):
        return False, "a .key file is never opened"
    return (True, "") if path.is_file() else (False, f"no {path.name} in the OpenCode data folder")


def _typed(e: sqlite3.Error) -> SourceError:
    """A SourceError for a SQLite failure, chosen by its error code and never by its message."""
    code = (getattr(e, "sqlite_errorcode", 0) or 0) & 0xFF
    if code in (_BUSY, _LOCKED):
        return SourceError(TIMEOUT, f"the OpenCode store stayed locked for {BUSY_MS / 1000:g} s")
    if code in (_CORRUPT, _NOTADB):
        return SourceError(BAD_FORMAT, "the OpenCode store isn't a readable SQLite file")
    if code in (_CANTOPEN, _PERM):
        return SourceError(UNREADABLE, "can't open the OpenCode store")
    return SourceError(FAILED, f"{type(e).__name__} while reading the OpenCode store")


def _wal_mode(path: Path) -> bool:
    """True when the file's header says WAL mode (bytes 18 and 19, the write and read versions, are both 2)."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return False
    return len(head) == 20 and head[:16] == b"SQLite format 3\x00" and head[18] == head[19] == 2


def _connect(path: Path) -> sqlite3.Connection:
    """The store, read-only. A path that isn't a regular file (a pipe planted under the name) is not opened."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise SourceError(UNAVAILABLE, f"{path.name} isn't a regular file")
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't read {path.name}: {e.strerror or type(e).__name__}") from e
    base = Path(os.path.abspath(path))
    # `mode=ro` on a WAL-mode store with no writer would create `-wal` and `-shm` and leave them behind, so that case is
    # opened `immutable=1`, which makes nothing. A `-wal` file means a writer is live: read through it (`mode=ro`).
    # A store that isn't in WAL mode makes no side file, and `immutable` would ignore a writer's lock.
    wal_mode = _wal_mode(base) and not base.with_name(base.name + "-wal").exists()
    uri = base.as_uri() + ("?immutable=1" if wal_mode else "?mode=ro")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_MS / 1000)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as e:
        raise _typed(e) from e
    return conn


def _check_layout(conn: sqlite3.Connection) -> None:
    for table, wanted in (("session", _SESSION), ("message", _MESSAGE)):
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not set(wanted) <= have:
            raise SourceError(BAD_FORMAT, f"the OpenCode store has no {table} table ws knows how to read")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _cost(value) -> float | None:
    """The cost OpenCode recorded; None for zero, because it records 0 for a model it has no price for."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    return float(value) if ok else None


def _state(conn: sqlite3.Connection, sid: str, updated_ms, now: float) -> str:
    """`working` while the latest message is unanswered or still being written and the session changed lately, else
    `idle`. Only the message's role and its completion time are read."""
    try:
        row = conn.execute(
            "SELECT json_extract(data, '$.role'), json_extract(data, '$.time.completed') FROM message "
            "WHERE session_id = ? ORDER BY time_created DESC, id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError as e:
        if (getattr(e, "sqlite_errorcode", 0) or 0) & 0xFF == _ERROR:  # a message row that isn't JSON
            return "unknown"
        raise
    fresh = isinstance(updated_ms, (int, float)) and now - updated_ms / 1000 <= WORKING_S
    if row is None or not fresh:
        return "idle"
    role, completed = row
    return "working" if role == "user" or (role == "assistant" and completed is None) else "idle"


def _read(path: Path, env) -> list[Session]:
    since_ms = int((env.now - WINDOW_S) * 1000)
    with closing(_connect(path)) as conn:
        _check_layout(conn)
        found = conn.execute(
            "SELECT id, directory, time_created, time_updated FROM session "
            "WHERE parent_id IS NULL AND time_archived IS NULL AND time_updated >= ? "
            "ORDER BY time_updated DESC LIMIT ?",
            (since_ms, MAX_SESSIONS),
        ).fetchall()
        out = []
        for sid, directory, created, updated in found:
            if not isinstance(sid, str) or not sid:
                continue
            cwd = _files.path(directory)
            shown = {
                "id": sid,
                "source": NAME,
                "runner": RUNNER,
                "cwd": cwd,
                "repo": _files.repo_label(cwd),
                "state": _state(conn, sid, updated, env.now),
                "started_at": _files.iso(created),
                "last_seen": _files.iso(updated),
            }
            if not records.open_path(cwd, env.cfg.roots):  # opaque or unplaced: nothing but the above is read
                out.append(Session(**shown, opaque=True))
                continue
            title, cost, t_in, t_out, t_read, t_write = conn.execute(
                "SELECT title, cost, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write "
                "FROM session WHERE id = ?",
                (sid,),
            ).fetchone()
            tokens = {
                "input": _count(t_in),
                "output": _count(t_out),
                "cache_creation": _count(t_write),
                "cache_read": _count(t_read),
            }
            cost_usd = _cost(cost)
            out.append(
                Session(
                    **shown,
                    name=_files.text(title, 80),
                    tokens=tokens if any(tokens.values()) else None,
                    cost_usd=cost_usd,
                    cost_basis="reported" if cost_usd is not None else records.NOT_MEASURED,
                )
            )
        return out


def read(kind, env):
    try:
        return _read(db_path(env), env)
    except sqlite3.Error as e:
        raise _typed(e) from e
