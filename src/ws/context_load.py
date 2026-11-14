"""Context loader: what a session is told when it starts, and what the first prompt adds, from one folder of plain files.

At SessionStart it prints the standing preferences, the area card and the index lines of the session's own data class,
under `BUDGET` characters. On the first prompt it adds the current records a full-text search on the message finds,
best first, and names their ids so the run record can say what context a run used. Advice, not a gate: an unreadable
folder or policy loads less, never more, and never fails the runner. CLAUDE.md imports stay the floor.

The folder is `WS_CONTEXT_DIR`, else `<data>/context`:

- `core/*.md`: the shared core (preferences, glossary, free and busy times). `core/preferences.md` is printed at start.
- `index/<name>.md`: one line per record. `index/<class>.md` belongs to a wall class; the rest are shared.
- `records/**/*.md`: front matter `id`, `status`, `title`, and optionally `class` and `area`.
- `areas.toml`: `[[area]]` with `name`, `folders`, `rank`, `state` (live, dormant, inbox) and optionally `now`, `left`, `dated`.

The wall: a walled session sees its own class's index and records and the shared core, nothing of another class. A shared
session never sees a record or index line of a walled class. A policy that will not load is the strictest case: core only.
An area: a record that names an area is offered only to a session working in that area; one that names none is shared.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ws import frontmatter, paths

BUDGET = 10_000  # characters printed at one event
MAX_HITS = 5
CARD_LINES = 5
_TERM = re.compile(r"\w+")
_MARK = "\n[ws context: cut at the size limit]\n"


@dataclass
class Who:
    """The caller: a walled class name (or None), whether the class is known, and the area name (or None)."""

    walled: str | None = None
    known: bool = True
    walls: frozenset[str] = frozenset()  # every walled class name in the policy
    area: str | None = None


@dataclass
class Rec:
    id: str
    title: str
    body: str
    path: str
    cls: str | None = None
    area: str | None = None


@dataclass
class Loaded:
    text: str = ""
    ids: list[str] = field(default_factory=list)


def root(env: dict | None = None) -> Path:
    e = paths.merged_environ() if env is None else env
    over = e.get("WS_CONTEXT_DIR")
    return Path(over) if over else paths.data_dir(e) / "context"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def who_is(cwd: str, env: dict | None = None) -> Who:
    """The caller's wall and area. A policy that will not load gives `known=False`."""
    from ws import policy

    w = Who()
    try:
        pol = policy.load()
        w.walls = frozenset(n for n, c in pol.classes.items() if c.access != "full")
        found = policy.class_of(pol, cwd)
        if found is policy.UNCLASSIFIED:
            pass
        elif found is not None and found.access != "full":
            w.walled = found.name
    except Exception:  # noqa: BLE001 - strictest case: core only
        w.known = False
    w.area = area_of(cwd, root(env))[0]
    return w


# ---- areas ------------------------------------------------------------------------------------------------------


def _areas(base: Path) -> list[dict]:
    try:
        data = tomllib.loads(_read(base / "areas.toml"))
    except tomllib.TOMLDecodeError:
        return []
    return [a for a in data.get("area", []) if isinstance(a, dict) and isinstance(a.get("name"), str)]


def _real(p: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(p))
    except (OSError, ValueError):
        return p


def area_of(cwd: str, base: Path) -> tuple[str | None, dict | None, list[dict]]:
    """(area name, its row, all rows) for the longest folder that holds `cwd`; (None, None, rows) if none does."""
    rows = _areas(base)
    here = _real(cwd)
    best: tuple[int, dict] | None = None
    for a in rows:
        for f in a.get("folders", []) if isinstance(a.get("folders"), list) else []:
            f = _real(str(f))
            if (here == f or here.startswith(f.rstrip(os.sep) + os.sep)) and (best is None or len(f) > best[0]):
                best = (len(f), a)
    return (best[1]["name"], best[1], rows) if best else (None, None, rows)


def _items(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])


def card(cwd: str, base: Path) -> str:
    """The area card, at most `CARD_LINES` lines. A folder in no area gets the inbox card and one line saying so."""
    name, row, rows = area_of(cwd, base)
    if not rows:
        return ""
    live = [a for a in rows if a.get("state", "live") == "live"]
    ranked = sorted(live, key=lambda a: (a.get("rank") is None, a.get("rank", 0)))
    lines: list[str] = []
    if row is None:
        row = next((a for a in rows if a.get("state") == "inbox" or a.get("name") == "inbox"), {"name": "inbox"})
        lines.append(f"Area: {row['name']} (this folder is in no area, so notes land in the inbox)")
    else:
        place = f", {ranked.index(row) + 1} of {len(ranked)}" if row in ranked else ""
        lines.append(f"Area: {row['name']}{place}")
    now = _items(row.get("now"))[:3]
    if now:
        lines.append("Now: " + "; ".join(now))
    if row.get("left"):
        lines.append(f"Left this week: {row['left']}")
    dated = _items(row.get("dated"))
    if dated:
        lines.append("Dated: " + "; ".join(dated))
    return "\n".join(lines[:CARD_LINES])


# ---- records ----------------------------------------------------------------------------------------------------


def _allowed_class(cls: str | None, who: Who) -> bool:
    if not who.known:
        return False
    if who.walled:
        return cls == who.walled
    return cls is None or cls not in who.walls


def _allowed(r: Rec, who: Who) -> bool:
    if not _allowed_class(r.cls, who):
        return False
    return r.area is None or r.area == who.area


def records(base: Path, who: Who) -> list[Rec]:
    """Current records this caller may see. A walled session sees only its own class's; core files are searchable by all."""
    out: list[Rec] = []
    for sub in ("records", "core"):
        folder = base / sub
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*.md")):
            doc = frontmatter.parse(_read(p))
            m = doc.meta
            if sub == "records" and str(m.get("status", "")).lower() != "current":
                continue
            rid = str(m.get("id") or p.stem)
            cls = m.get("class") if isinstance(m.get("class"), str) else None
            area = m.get("area") if isinstance(m.get("area"), str) else None
            if sub == "core":
                cls = area = None
            r = Rec(rid, str(m.get("title") or p.stem), doc.body.strip(), str(p), cls, area)
            if _allowed(r, who):
                out.append(r)
    return out


def _terms(text: str) -> list[str]:
    seen: list[str] = []
    for t in _TERM.findall(text.lower()):
        if len(t) > 2 and t not in seen:
            seen.append(t)
    return seen[:24]


def search(recs: list[Rec], message: str, limit: int = MAX_HITS) -> list[Rec]:
    """Records matching the message, best first (FTS5 bm25; a plain term count where SQLite has no FTS5)."""
    terms = _terms(message)
    if not terms or not recs:
        return []
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE d USING fts5(title, body, tokenize='porter unicode61')")
            conn.executemany(
                "INSERT INTO d(rowid, title, body) VALUES (?,?,?)", [(i, r.title, r.body) for i, r in enumerate(recs)]
            )
            for joiner in (" ", " OR "):
                q = joiner.join(f'"{t}"' for t in terms)
                rows = conn.execute(
                    "SELECT rowid FROM d WHERE d MATCH ? ORDER BY bm25(d, 5.0, 1.0), rowid LIMIT ?", (q, limit)
                ).fetchall()
                if rows:
                    return [recs[i] for (i,) in rows]
            return []
        finally:
            conn.close()
    except sqlite3.Error:
        scored = [(sum(t in f"{r.title} {r.body}".lower() for t in terms), i) for i, r in enumerate(recs)]
        return [recs[i] for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s][:limit]


def _index_lines(base: Path, who: Who) -> str:
    folder = base / "index"
    if not folder.is_dir() or not who.known:
        return ""
    names = sorted(p for p in folder.glob("*.md"))
    if who.walled:
        names = [p for p in names if p.stem == who.walled]
    else:
        names = [p for p in names if p.stem not in who.walls]
    return "\n".join(t.strip() for p in names if (t := _read(p)).strip())


def _fit(text: str) -> str:
    return text if len(text) <= BUDGET else text[: BUDGET - len(_MARK)] + _MARK


# ---- the two events ---------------------------------------------------------------------------------------------


def at_start(cwd: str, env: dict | None = None) -> Loaded:
    """SessionStart: standing preferences, the area card, the index lines of the caller's class. Under `BUDGET`."""
    base = root(env)
    who = who_is(cwd, env)
    parts = []
    prefs = _read(base / "core" / "preferences.md")
    if prefs.strip():
        parts.append("Standing preferences:\n" + frontmatter.parse(prefs).body.strip())
    c = card(cwd, base)
    if c:
        parts.append(c)
    idx = _index_lines(base, who)
    if idx:
        parts.append("Index:\n" + idx)
    return Loaded(_fit("\n\n".join(parts)) if parts else "")


def at_prompt(message: str, cwd: str, env: dict | None = None) -> Loaded:
    """First prompt: the current records a search on the message finds, with their ids. Under `BUDGET`."""
    base = root(env)
    who = who_is(cwd, env)
    hits = search(records(base, who), message)
    if not hits:
        return Loaded()
    body = "\n\n".join(f"[{r.id}] {r.title}\n{r.body}" for r in hits)
    ids = [r.id for r in hits]
    text = f"Records found for this message (ids: {', '.join(ids)}):\n{body}"
    return Loaded(_fit(text), ids)
