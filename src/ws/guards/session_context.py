"""session-context: when a session starts, tell it which of the project's state files exist, one line each, so it reads
them before it asks. The project is the git top level above the session's folder, and the main worktree too when the
session runs in a linked one. `files` lists the paths to look for, relative to either (default
`.claude-state/STATE.md`).

Two more lines, each short and plain. With `warn_api_key` (default true), an interactive session that has
`ANTHROPIC_API_KEY` set is told it bills that key, not the subscription; the value is never printed. A headless
`claude -p` or SDK session, and a `ws run` job (its key is the lease placeholder), get no line. And once for each ws
version in each project (a start that carries a session id), a session is told which ws version it runs and up to four lines of what that version
changed, read from a `CHANGELOG.md` shipped with ws when there is one (else the version line alone). That record is
`$WS_HOME/sessions/project-HASH.session-context.json`.

The runner adds the text to the session's context at start.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from ws.guards import Finding, _state, known_keys, text_list
from ws.policy import PolicyError

NAME = "session-context"
EVENTS = ("start",)
KINDS = ()

DEFAULT_FILES = (".claude-state/STATE.md",)
STATE = "session-context"
LEASE_PLACEHOLDER = "ws-lease-placeholder"  # the key a `ws run` job holds: the broker swaps in the real one
HEADLESS_ENTRYPOINT = "sdk"  # CLAUDE_CODE_ENTRYPOINT starts with this for `claude -p` and the SDKs
CHANGE_LINES = 4  # lines of changelog after the version line: 5 in all
CHANGE_WIDTH = 160
_MAX_GITFILE = 4096
_MAX_CHANGELOG = 262144
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Config:
    files: tuple[str, ...] = DEFAULT_FILES
    warn_api_key: bool = True


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("files", "warn_api_key"))
    warn = table.get("warn_api_key", True)
    if not isinstance(warn, bool):
        raise PolicyError(f"{where}: warn_api_key must be true or false")
    files = text_list(table, "files", where, DEFAULT_FILES)
    for f in files:
        n = posixpath.normpath(f)
        if f.startswith(("/", "~")) or n == "." or n.startswith("..") or "\n" in f:
            raise PolicyError(f"{where}: files: {f!r} must be a path inside the project (relative, no `..`)")
    return Config(files, warn)


def _read_small(path: Path, limit: int = _MAX_GITFILE) -> str:
    with path.open("rb") as f:
        return f.read(limit).decode("utf-8", "replace").strip()


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


def _headless(env) -> bool:
    """True for a session nobody is typing into: `claude -p` and the SDKs, or a `ws run` job on a leased key."""
    return env.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(HEADLESS_ENTRYPOINT) or (
        env.get("ANTHROPIC_API_KEY") == LEASE_PLACEHOLDER
    )


def _key_line(env) -> str | None:
    """One line when an interactive session bills an API key. It never holds any part of the key."""
    if not env.get("ANTHROPIC_API_KEY", "").strip() or _headless(env):
        return None
    return "ws: ANTHROPIC_API_KEY is set, so this session bills that key, not your Claude subscription."


def _clean(text: str) -> str:
    return _CONTROL.sub(" ", text).strip()[:CHANGE_WIDTH]


def changelog_lines(version: str, text: str, limit: int = CHANGE_LINES) -> list[str]:
    """The first `limit` bullet or text lines under the `## [version]` heading of a Keep a Changelog file."""
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        if raw.startswith("## "):
            if inside:
                break
            inside = re.match(rf"##\s+\[?{re.escape(version)}\]?(?:\s|$)", raw) is not None
            continue
        line = _clean(raw.lstrip("-* \t"))
        if inside and line and not raw.startswith("#") and len(out) < limit:
            out.append(line)
    return out


def _shipped_changelog() -> str:
    """The text of a CHANGELOG.md shipped with ws (inside the package, or at the top of a source tree), else ''."""
    here = Path(__file__).resolve().parents[1]
    tree = here.parents[1]
    for path in (here / "CHANGELOG.md", tree / "CHANGELOG.md"):
        try:
            if path.is_file() and (path.parent == here or (tree / "pyproject.toml").is_file()):
                return _read_small(path, _MAX_CHANGELOG)
        except OSError:
            continue
    return ""


def _project_key(roots: list[Path]) -> str:
    return "project-" + hashlib.sha256(str(roots[-1].resolve()).encode()).hexdigest()[:24]


def _news(roots: list[Path], ctx) -> str | None:
    """The version line (and what changed) the first time a project's session starts on this ws version."""
    from ws import __version__

    version = _clean(__version__.split("+", 1)[0])
    known: list[str] = []

    def seen(state: dict) -> None:
        known.append(state.get("version") if isinstance(state.get("version"), str) else "")
        if known[0] != version:
            state["version"] = version

    _state(ctx.home, _project_key(roots), STATE, seen)
    if known[0] == version:
        return None
    head = f"ws: now on {version} (this project last saw {known[0]})" if known[0] else f"ws: version {version}"
    return "\n".join([head, *changelog_lines(version, _shipped_changelog())])


def judge(call, cfg: Config, ctx) -> Finding | None:
    lines: list[str] = []
    roots: list[Path] = []
    if ctx.cwd:
        start = Path(ctx.cwd)
        top = _top(start) or start
        roots = [top]
        main = _main_worktree(top)
        if main is not None:
            roots.append(main)
        for rel in cfg.files:
            for root in roots:
                path = root / rel
                if path.is_file():
                    lines.append(f"ws: {rel} exists at {path}" + (" (main worktree)" if root is main else ""))
    if cfg.warn_api_key and (line := _key_line(os.environ)):
        lines.append(line)
    if roots and ctx.session_id:  # the runner's start hook always sends an id; a bare Context records nothing
        try:
            news = _news(roots, ctx)
        except (OSError, ValueError):  # advice only: no record means no news line, never a failed start
            news = None
        if news:
            lines.append(news)
    return Finding("note", "\n".join(lines)) if lines else None
