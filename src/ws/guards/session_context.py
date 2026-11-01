"""session-context: when a session starts, tell it which of the project's state files exist, one line each, so it reads
them before it asks. The project is the git top level above the session's folder, and the main worktree too when the
session runs in a linked one. `files` lists the paths to look for, relative to either (default
`.claude-state/STATE.md`).

The runner adds the text to the session's context at start.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "session-context"
EVENTS = ("start",)
KINDS = ()

DEFAULT_FILES = (".claude-state/STATE.md",)
_MAX_GITFILE = 4096


@dataclass(frozen=True)
class Config:
    files: tuple[str, ...] = DEFAULT_FILES


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("files",))
    files = text_list(table, "files", where, DEFAULT_FILES)
    for f in files:
        n = posixpath.normpath(f)
        if f.startswith(("/", "~")) or n == "." or n.startswith("..") or "\n" in f:
            raise PolicyError(f"{where}: files: {f!r} must be a path inside the project (relative, no `..`)")
    return Config(files)


def _read_small(path: Path) -> str:
    with path.open("rb") as f:
        return f.read(_MAX_GITFILE).decode("utf-8", "replace").strip()


def _top(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _main_worktree(top: Path) -> Path | None:
    """The main worktree's folder when `top` is a linked worktree (its `.git` is a file naming a git folder whose
    `commondir` points at the main one), else None."""
    dot = top / ".git"
    try:
        if not dot.is_file():
            return None
        line = _read_small(dot)
        if not line.startswith("gitdir:"):
            return None
        gitdir = (top / line.removeprefix("gitdir:").strip()).resolve()
        common = (gitdir / _read_small(gitdir / "commondir")).resolve()
    except OSError:
        return None
    main = common.parent if common.name == ".git" else None
    return main if main and main != top else None


def judge(call, cfg: Config, ctx) -> Finding | None:
    if not ctx.cwd:
        return None
    start = Path(ctx.cwd)
    top = _top(start) or start
    roots = [top]
    main = _main_worktree(top)
    if main is not None:
        roots.append(main)
    lines = []
    for rel in cfg.files:
        for root in roots:
            path = root / rel
            if path.is_file():
                lines.append(f"ws: {rel} exists at {path}" + (" (main worktree)" if root is main else ""))
    return Finding("note", "\n".join(lines)) if lines else None
