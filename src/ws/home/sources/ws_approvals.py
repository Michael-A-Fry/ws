"""Approvals waiting for a person: the gate's pending records (`$WS_HOME/approvals/pending`), one ask each.

Granting needs a real terminal (the approve command), so each ask is answered in a terminal, whatever the app.
"""

from __future__ import annotations

from ws import approvals
from ws.home import records
from ws.home.records import Ask
from ws.home.sources import UNREADABLE, SourceError, _files

NAME = "ws_approvals"
PROVIDES = ("asks",)
ORDER = 50


def available(env):
    folder = env.ws_home / "approvals" / "pending"
    return (True, "") if folder.is_dir() else (False, "no pending approvals folder")


def read(kind, env):
    try:
        rows = approvals.pending(env.ws_home / "approvals", env.now)
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't read pending approvals: {e.strerror or type(e).__name__}") from e
    out = []
    for ident, bound, left in rows:
        cwd = _files.path(bound.cwd)
        out.append(
            Ask(
                id=ident,
                source=NAME,
                kind="approval",
                session_id=bound.session,
                repo=_files.repo_label(cwd),
                title=_files.text(f"{bound.kind}: {bound.value}", 200),
                decision=f"ws approve {ident[:12]}",
                created=_files.iso(env.now + left - approvals.TTL),
                opaque=records.under(cwd, env.cfg.roots),
                placed=records.open_path(cwd, env.cfg.roots),
                answer_in="terminal",
            )
        )
    return out
