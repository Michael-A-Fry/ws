"""Memory module: Markdown files with front matter in a git repo, plus a rebuildable SQLite FTS5 index.

The files are the truth. The index (`$WS_HOME/index/memory.sqlite`) only helps search; delete it and the next search
rebuilds the same one. Search covers memory and your notes folder. ws writes new memory files and never edits one.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ws import frontmatter, output
from ws.notes import WsError, clean_tags, read_text, role_path, title_slug, write_new
from ws.profile import ProfileError, ws_home

KINDS = ("fact", "decision", "preference", "person", "project")
MAX_BYTES = 5_000_000  # a Markdown file bigger than this is skipped
SCHEMA = 1
_TERM = re.compile(r"\w+")
_warned = False


@dataclass
class Hit:
    source: str  # "memory" or "notes"
    path: str
    title: str
    snippet: str
    score: float


@dataclass
class Stats:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0


# ---- git ---------------------------------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise WsError("unavailable", "git is not installed or not on PATH", "install git") from e


def _git_fail(what: str, p: subprocess.CompletedProcess) -> WsError:
    lines = (p.stderr or p.stdout or "").strip().splitlines()
    return WsError("failed", f"git {what} failed: {lines[0] if lines else 'no output'}")


def is_repo(root: Path) -> bool:
    return root.is_dir() and (root / ".git").exists()


def init(root: Path) -> bool:
    """Make root a git repo if it is not one. True if anything changed."""
    changed = not root.is_dir()
    root.mkdir(parents=True, exist_ok=True)
    if is_repo(root):
        return changed
    p = _git(root, "init", "-q", "-b", "main")
    if p.returncode != 0:
        raise _git_fail("init", p)
    for key, value in (("user.name", "ws"), ("user.email", "ws@localhost")):
        if not _git(root, "config", key).stdout.strip():  # only when git has no identity of its own
            _git(root, "config", "--local", key, value)
    return True


# ---- writing -----------------------------------------------------------------------------------------------------


def add(
    text: str,
    title: str | None = None,
    kind: str = "fact",
    tags: list[str] | None = None,
    source: str = "cli",
    env: dict | None = None,
    now: datetime | None = None,
) -> Path:
    """Write one memory file and commit it. Returns the path."""
    if kind not in KINDS:
        raise WsError("bad_input", f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    text = text.strip()
    if not text:
        raise WsError("bad_input", "nothing to remember: the text is empty")
    root = role_path("memory", env)
    if not is_repo(root):
        raise WsError("not_found", f"{root} is not a git repo", "run `ws memory init`")
    now = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    meta: dict = {"id": str(uuid.uuid4()), "created": now.replace(microsecond=0), "kind": kind}
    if title and title.strip():
        meta["title"] = title.strip()
    meta.update({"tags": clean_tags(tags), "source": source})
    try:
        path = write_new(root / f"{kind}s", title_slug(title, text), frontmatter.dumps(meta, text))
    except OSError as e:
        raise WsError("io", f"could not write to {root}: {e.strerror or type(e).__name__}") from e
    rel = path.relative_to(root).as_posix()
    p = _git(root, "add", "--", rel)
    if p.returncode == 0:
        p = _git(root, "commit", "-q", "-m", f"memory: add {path.stem}", "--", rel)
    if p.returncode != 0:
        raise WsError("failed", f"{path} was written but not committed: {_git_fail('commit', p).message}")
    return path


# ---- reading files -----------------------------------------------------------------------------------------------


def walk(root: Path):
    """Every .md file under root, in a stable order, skipping dot-folders (.git, .obsidian, .trash)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.endswith(".md") and not name.startswith(".") and p.is_file():  # not a FIFO, socket or device
                yield p


def parse_file(path: Path, data: bytes) -> tuple[str, str, str]:
    """(title, body, tags) of a Markdown file. Front matter ws cannot read still leaves a usable file."""
    doc = frontmatter.parse(data.decode("utf-8", errors="replace"))
    title = doc.meta.get("title")
    title = str(title).strip() if title is not None else ""
    if not title:
        heading = re.search(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", doc.body, re.MULTILINE)
        title = heading.group(1) if heading else path.stem
    tags = doc.meta.get("tags")
    if isinstance(tags, list):
        tags = " ".join(str(t) for t in tags)
    return title, doc.body, str(tags) if tags else ""


def configured_roots(env: dict | None = None) -> list[tuple[str, Path]]:
    """[(source, folder)] for the memory and notes folders that are set in the profile and exist."""
    roots: list[tuple[str, Path]] = []
    problem = ""
    for source, role in (("memory", "memory"), ("notes", "notes")):
        try:
            p = role_path(role, env)
        except WsError as e:
            problem = problem or e.message
            continue
        if p.is_dir():
            roots.append((source, p))
    if not roots:
        raise WsError("not_found", problem or "neither a memory nor a notes folder exists", "run `ws doctor`")
    return roots


# ---- the index ---------------------------------------------------------------------------------------------------


def index_path(env: dict | None = None) -> Path:
    return ws_home(env) / "index" / "memory.sqlite"


def has_fts5() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        try:
            c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        finally:
            c.close()
    except sqlite3.Error:
        return False
    return True


def _warn_no_fts5() -> None:
    global _warned
    if not _warned:
        _warned = True
        print("ws: this SQLite build has no FTS5, so search scans the files (slower)", file=sys.stderr)


def _open(db: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=10)
    stale = conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA
    if stale:
        conn.executescript("DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS files;")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, source TEXT NOT NULL,"
        " mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL);"
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(title, body, tags, path, source UNINDEXED,"
        " tokenize = 'porter unicode61 remove_diacritics 2');"
    )
    if stale:
        conn.execute(f"PRAGMA user_version = {SCHEMA}")
    return conn


def refresh(conn: sqlite3.Connection, roots: list[tuple[str, Path]]) -> Stats:
    """Bring the index in line with the files: new or changed ones in (mtime+size, then sha256), gone ones out."""
    stats = Stats()
    rows = {r[1]: r for r in conn.execute("SELECT id, path, mtime_ns, size, sha256, source FROM files")}
    seen: set[str] = set()
    for source, given in roots:
        root = given.resolve()  # the keys are absolute whatever the caller passed
        for p in walk(root):
            key = str(p)
            if key in seen:  # a notes folder inside the memory folder is indexed once
                continue
            try:
                st = p.stat()
            except OSError:
                stats.skipped += 1
                continue
            seen.add(key)
            row = rows.get(key)
            if row and row[2] == st.st_mtime_ns and row[3] == st.st_size and row[5] == source:
                stats.unchanged += 1
                continue
            if st.st_size > MAX_BYTES:
                stats.skipped += 1
                seen.discard(key)
                continue
            try:
                data = p.read_bytes()
            except OSError:
                stats.skipped += 1
                seen.discard(key)
                continue
            sha = hashlib.sha256(data).hexdigest()
            if row and row[4] == sha and row[5] == source:
                conn.execute(
                    "UPDATE files SET mtime_ns = ?, size = ? WHERE id = ?", (st.st_mtime_ns, st.st_size, row[0])
                )
                stats.unchanged += 1
                continue
            title, body, tags = parse_file(p, data)
            if row:
                conn.execute("DELETE FROM docs WHERE rowid = ?", (row[0],))
                conn.execute(
                    "UPDATE files SET source = ?, mtime_ns = ?, size = ?, sha256 = ? WHERE id = ?",
                    (source, st.st_mtime_ns, st.st_size, sha, row[0]),
                )
                rowid = row[0]
            else:
                cur = conn.execute(
                    "INSERT INTO files (path, source, mtime_ns, size, sha256) VALUES (?, ?, ?, ?, ?)",
                    (key, source, st.st_mtime_ns, st.st_size, sha),
                )
                rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO docs (rowid, title, body, tags, path, source) VALUES (?, ?, ?, ?, ?, ?)",
                (rowid, title, body, tags, p.relative_to(root).as_posix(), source),
            )
            stats.indexed += 1
    for key, row in rows.items():
        if key not in seen:
            conn.execute("DELETE FROM docs WHERE rowid = ?", (row[0],))
            conn.execute("DELETE FROM files WHERE id = ?", (row[0],))
            stats.removed += 1
    conn.commit()
    return stats


def reindex(env: dict | None = None, full: bool = False) -> tuple[Stats, Path]:
    """Update the index (or rebuild it from scratch with full=True)."""
    roots = configured_roots(env)
    db = index_path(env)
    if not has_fts5():
        _warn_no_fts5()
        return Stats(), db
    if full:
        db.unlink(missing_ok=True)
    return _with_index(db, lambda conn: refresh(conn, roots)), db


def _corrupt(e: sqlite3.DatabaseError) -> bool:
    """True when SQLite says the file itself is damaged (SQLITE_CORRUPT 11, SQLITE_NOTADB 26)."""
    if getattr(e, "sqlite_errorcode", 0) & 0xFF in (11, 26):
        return True
    text = str(e).lower()
    return any(s in text for s in ("malformed", "not a database", "disk image"))


def _with_index(db: Path, fn):
    """Run fn(conn) on the index. A broken index file is deleted and rebuilt once: it is only a cache."""
    db.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        conn = None
        try:
            conn = _open(db)
            return fn(conn)
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
                raise WsError("unavailable", "the search index is busy", "try again in a moment") from e
            if attempt == 2 or not _corrupt(e):  # only a damaged file is deleted; anything else is left alone
                raise WsError(
                    "failed", f"the search index failed ({type(e).__name__})", "`ws memory reindex --full` rebuilds it"
                ) from e
            if conn:
                conn.close()
                conn = None
            db.unlink(missing_ok=True)
        finally:
            if conn:
                conn.close()


def check_index(root: Path) -> tuple[bool, str]:
    """For doctor: does an index of this folder build? Uses a throwaway in-memory database."""
    if not has_fts5():
        return True, "no FTS5 in this SQLite: search will scan the files"
    try:
        conn = _open(":memory:")
        try:
            stats = refresh(conn, [("memory", root)])
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, f"the search index would not build ({type(e).__name__})"
    n = stats.indexed + stats.unchanged
    return True, f"{n} file{'' if n == 1 else 's'} index fine"


# ---- searching ---------------------------------------------------------------------------------------------------


def _terms(query: str) -> list[str]:
    terms = _TERM.findall(query.lower())
    if not terms:
        raise WsError("bad_input", "nothing to search for: the query has no words")
    return terms


def _clean(s: str) -> str:
    return " ".join((s or "").split())


def _fts_search(conn: sqlite3.Connection, terms: list[str], limit: int) -> list[Hit]:
    sql = (
        "SELECT f.source, f.path, docs.title, snippet(docs, -1, '[', ']', '...', 14), bm25(docs, 5.0, 1.0, 3.0, 1.0, 0.0)"
        " FROM docs JOIN files f ON f.id = docs.rowid WHERE docs MATCH ?"
        " ORDER BY bm25(docs, 5.0, 1.0, 3.0, 1.0, 0.0), f.path LIMIT ?"
    )
    for joiner in (" ", " OR "):  # every word first; any word if that finds nothing
        match = joiner.join(f'"{t}"' for t in terms)
        rows = conn.execute(sql, (match, limit)).fetchall()
        if rows:
            return [Hit(s, p, t, _clean(sn), round(-score, 6)) for s, p, t, sn, score in rows]
    return []


def _scan_search(roots: list[tuple[str, Path]], terms: list[str], limit: int) -> list[Hit]:
    """The no-FTS5 fallback: read every file and count the words. Same shape as the indexed search."""
    docs = []
    seen: set[str] = set()
    for source, root in roots:
        for p in walk(root):
            if str(p) in seen:
                continue
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
                title, body, tags = parse_file(p, p.read_bytes())
            except OSError:
                continue
            seen.add(str(p))
            docs.append((source, p, p.relative_to(root).as_posix(), title, body, tags))

    def rank(need_all: bool) -> list[Hit]:
        hits = []
        for source, p, rel, title, body, tags in docs:
            counts = [
                5 * title.lower().count(t) + 3 * tags.lower().count(t) + body.lower().count(t) + rel.lower().count(t)
                for t in terms
            ]
            if all(counts) if need_all else any(counts):
                low = body.lower()
                at = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
                snippet = _clean(body[max(0, at - 60) : at + 100])
                hits.append(Hit(source, str(p), title, snippet, float(sum(counts))))
        hits.sort(key=lambda h: (-h.score, h.path))
        return hits[:limit]

    return rank(True) or rank(False)


def search(query: str, limit: int = 10, env: dict | None = None) -> list[Hit]:
    """Search memory and notes. A match needs every word; if nothing matches, any word will do."""
    terms = _terms(query)
    limit = max(1, min(int(limit), 100))
    roots = configured_roots(env)
    if not has_fts5():
        _warn_no_fts5()
        return _scan_search(roots, terms, limit)

    def run(conn: sqlite3.Connection) -> list[Hit]:
        refresh(conn, roots)
        return _fts_search(conn, terms, limit)

    return _with_index(index_path(env), run)


def render_hits(hits: list[Hit]) -> str:
    out = []
    for h in hits:
        out.append(f"{h.source:<7} {h.title}  {h.path}")
        if h.snippet:
            out.append(f"        {h.snippet}")
    return "\n".join(out)


# ---- the command -------------------------------------------------------------------------------------------------


def _guard(fn):
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ProfileError as e:
            raise WsError("bad_config", str(e), "fix or remove the profile") from e

    return run


@_guard
def cmd_init(args: argparse.Namespace) -> int:
    root = role_path("memory")
    changed = init(root)
    if args.json:
        output.result({"path": str(root), "changed": changed})
        return 0
    print(f"ws: {'made ' + str(root) + ' a git repo' if changed else '0 changes to ' + str(root)}")
    return 0


@_guard
def cmd_add(args: argparse.Namespace) -> int:
    path = add(read_text(args.text), args.title, args.kind, args.tag)
    if args.json:
        output.result({"path": str(path)})
        return 0
    print(path)
    return 0


@_guard
def cmd_search(args: argparse.Namespace) -> int:
    hits = search(" ".join(args.query), args.limit)
    if args.json:
        output.result([asdict(h) for h in hits])
    elif hits:
        print(render_hits(hits))
    else:
        print("ws: no matches", file=sys.stderr)
    return 0


@_guard
def cmd_reindex(args: argparse.Namespace) -> int:
    stats, db = reindex(full=args.full)
    if not has_fts5():
        if args.json:
            output.result({"indexed": 0, "fts5": False})
            return 0
        print("ws: nothing to index without FTS5; search scans the files")
        return 0
    if args.json:
        output.result({"indexed": stats.indexed, "unchanged": stats.unchanged, "removed": stats.removed, "db": str(db)})
        return 0
    print(f"ws: indexed {stats.indexed}, unchanged {stats.unchanged}, removed {stats.removed} ({db})")
    return 0


def register(sub) -> None:
    m = sub.add_parser("memory", help="remember things in Markdown + git, search them and your notes")
    ms = m.add_subparsers(dest="memory_command", required=True)

    i = ms.add_parser("init", help="make the memory folder a git repo (0 changes if it already is)")
    i.set_defaults(func=cmd_init)

    a = ms.add_parser("add", help="write one memory file and commit it")
    a.add_argument("text", nargs="+", help="what to remember, or `-` to read it from stdin")
    a.add_argument("--title", help="a title (also names the file)")
    a.add_argument("--kind", choices=KINDS, default="fact", help="default fact")
    a.add_argument("--tag", action="append", metavar="TAG", help="a tag; repeat for more")
    a.set_defaults(func=cmd_add)

    s = ms.add_parser("search", help="search memory and notes")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=10, metavar="N", help="most results (default 10)")
    s.set_defaults(func=cmd_search)

    r = ms.add_parser("reindex", help="update the search index (it is only a cache)")
    r.add_argument("--full", action="store_true", help="throw it away and rebuild from the files")
    r.set_defaults(func=cmd_reindex)
