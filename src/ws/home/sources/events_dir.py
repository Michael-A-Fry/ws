"""Events from a folder of daily JSON-lines files of CloudEvents (`<UTC date>.jsonl`).

A line has `id`, `time`, `source` and `type`, and `data` with `session_id`, `cwd` and `repo`. The summary is built from
the type and the repo label only; the rest of `data` is not read. An event whose folder or repo is under an opaque path
is opaque, and the file's own word never places one: it is open only on a session in the snapshot that is open.
The folder is `[home.sources] events_dir`; only the days from the snapshot's `since` on are read, and at most
the newest 500 events are kept.
"""

from __future__ import annotations

from ws.home import records
from ws.home.records import Event
from ws.home.sources import _files

NAME = "events_dir"
PROVIDES = ("events",)
ORDER = 90
KEEP = 500


def available(env):
    return (True, "") if env.path("events_dir").is_dir() else (False, "no events folder at the configured path")


def read(kind, env):
    out = []
    for path in _files.day_files(env.path("events_dir"), env.event_since):
        for line in _files.iter_jsonl(path):
            ident, etype, when = line.get("id"), line.get("type"), _files.iso(line.get("time"))
            if not (isinstance(ident, str) and isinstance(etype, str) and when):
                continue
            data = line.get("data") if isinstance(line.get("data"), dict) else {}
            repo = _files.text(data.get("repo"), 120)
            cwd = _files.path(data.get("cwd"))
            out.append(
                Event(
                    id=ident,
                    ts=when,
                    source=_files.text(line.get("source"), 80),
                    type=_files.text(etype, 80),
                    session_id=_files.text(data.get("session_id"), 128),
                    summary=f"{_files.text(etype, 80)} in {repo}" if repo else _files.text(etype, 80),
                    opaque=records.under(cwd, env.cfg.roots) or records.label_matches(repo, env.cfg.roots),
                )
            )
    return sorted(out, key=lambda e: e.ts)[-KEEP:]
