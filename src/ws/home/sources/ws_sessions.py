"""Sessions ws recorded itself: `$WS_HOME/sessions/<id>.json`, one file per session, written by `ws session start`."""

from __future__ import annotations

from ws.home.records import Session
from ws.home.sources import _files

NAME = "ws_sessions"
PROVIDES = ("sessions",)
ORDER = 40


def available(env):
    folder = env.ws_home / "sessions"
    return (True, "") if folder.is_dir() else (False, "no sessions folder in the ws home")


def read(kind, env):
    out = []
    for path in _files.json_files(env.ws_home / "sessions"):
        rec = _files.read_object(path)
        # a guard's own record for a session has no `release`; only the session record does
        if not rec or not isinstance(rec.get("session_id"), str) or not isinstance(rec.get("release"), str):
            continue
        cwd = _files.text(rec.get("cwd"), 500)
        out.append(
            Session(
                id=rec["session_id"],
                source=NAME,
                runner=_files.text(rec.get("runner"), 40) or "unknown",
                cwd=cwd,
                repo=_files.repo_label(cwd),
                started_at=_files.iso(rec.get("started_at")),
                last_seen=_files.iso(rec.get("ts")),
            )
        )
    return out
