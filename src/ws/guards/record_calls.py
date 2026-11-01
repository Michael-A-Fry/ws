"""record-calls: append each tool call a session makes to `$WS_HOME/logs/gate-calls.jsonl` as `{at, runner, payload}`,
so `ws gate replay` can run the same calls against a changed policy. It only records: a log it can't write is a
warning. The log has the bounds every ws log has (ws.jsonl: long fields cut, the file rotates, writers take a lock).

A payload holds the command text as the session typed it, so a value typed into a command is in the log. Off unless the
policy names it in `[guards] on`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl
from ws.guards import Finding, known_keys
from ws.profile import ws_home

NAME = "record-calls"
EVENTS = ("pre",)
KINDS = ("command", "read", "search", "write", "tool", "agent", "skill", "message")

LOG = "gate-calls.jsonl"


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def log_path(home: Path | None = None) -> Path:
    return (home if home is not None else ws_home()) / "logs" / LOG


def judge(call, cfg, ctx) -> Finding | None:
    if ctx.memo.get(NAME):
        return None  # one line per hook run, however many calls the payload held: replay reads the payload back whole
    ctx.memo[NAME] = True
    stamp = datetime.fromtimestamp(ctx.now(), UTC).isoformat(timespec="seconds")
    try:
        jsonl.append(log_path(ctx.home), {"at": stamp, "runner": ctx.runner, "payload": ctx.payload})
    except OSError as e:  # recording is a convenience: a log that cannot be written never stops the call
        return Finding("warn", f"record-calls could not write {LOG} ({type(e).__name__})")
    return None
