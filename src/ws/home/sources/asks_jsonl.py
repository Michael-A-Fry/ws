"""Questions and permissions sessions put to a person: a JSON-lines file of events, folded into the ones still open.

Each line has `event` (`added`, `answered`, `withdrawn` or `done`), `id` and `ts`. An `added` line carries `repo`,
`session` ({name, tmux}), `question`, `options` ([{label, description}], the first the recommended one), `decision`,
`source` (`permission` for a permission, else a question), `rc_url` and `redacted`. The file's own word never places an
ask: one is open only on a session in the snapshot that is open. A later `answered`, `withdrawn` or
`done` line closes the ask. The path is `[home.sources] asks_jsonl`.
"""

from __future__ import annotations

from ws.home import records
from ws.home.records import Ask
from ws.home.sources import _files

NAME = "asks_jsonl"
PROVIDES = ("asks",)
ORDER = 50
CLOSING = ("answered", "withdrawn", "done")
MAX_OPTIONS = 8


def available(env):
    path = env.path("asks_jsonl")
    return (True, "") if path.is_file() else (False, "no asks file at the configured path")


def _options(raw) -> tuple:
    out = []
    for o in raw if isinstance(raw, list) else []:
        label = _files.text(o.get("label"), 120) if isinstance(o, dict) else ""
        if label:
            out.append({"label": label, "description": _files.text(o.get("description"), 300)})
    return tuple(out[:MAX_OPTIONS])


def _link(value) -> str:
    return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 2000 else ""


def read(kind, env):
    open_asks: dict[str, Ask] = {}
    for line in _files.iter_jsonl(env.path("asks_jsonl")):
        ident, event = line.get("id"), line.get("event")
        if not isinstance(ident, str) or not ident:
            continue
        if event in CLOSING:
            open_asks.pop(ident, None)
        elif event == "added":
            repo_raw = _files.path(line.get("repo"))
            is_path = "/" in repo_raw or "\\" in repo_raw  # a bare name is a label, which has no folder to test
            session = line.get("session") if isinstance(line.get("session"), dict) else {}
            link = _link(line.get("rc_url"))
            open_asks[ident] = Ask(
                id=ident,
                source=NAME,
                kind="permission" if line.get("source") == "permission" else "question",
                session_id=_files.text(session.get("session_id") or session.get("id"), 128),
                session_name=_files.text(session.get("name"), 80),
                tmux=_files.text(session.get("tmux"), 80),
                repo=_files.repo_label(repo_raw) if is_path else _files.text(repo_raw, _files.LABEL_CAP),
                title=_files.text(line.get("question"), 300),
                options=_options(line.get("options")),
                decision=_files.text(line.get("decision"), 300),
                created=_files.iso(line.get("ts")),
                # only an explicit `redacted: false` is visible: a line that doesn't say is treated as redacted
                opaque=line.get("redacted") is not False or (is_path and records.under(repo_raw, env.cfg.roots)),
                answer_in="app" if link else "terminal",
                rc_url=link,
            )
    return list(open_asks.values())
