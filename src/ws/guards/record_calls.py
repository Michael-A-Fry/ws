"""record-calls: append each tool call a session makes to `$WS_HOME/logs/gate-calls.jsonl` as `{at, runner, payload}`,
so `ws gate replay` can run the same calls against a changed policy. It only records: a log it can't write is a
warning. The log has the bounds every ws log has (ws.jsonl: long fields cut, the file rotates, writers take a lock).

What a line holds: the hook payload as the runner sent it (tool name, folder, session id and the tool's input: a
command, a path, a search pattern, an agent prompt, a message) with one change. The text a write tool carries is
replaced by its size and fingerprint, `{"bytes": N, "sha256": "..."}`: Write `content`, Edit `old_string` and
`new_string`, each MultiEdit edit's two strings, NotebookEdit `new_source` (OpenCode's `content`, `oldString`,
`newString` and `edits` likewise). The path stays, so the file a call wrote is on record and the file's text is not.
Replay judges a call by its tool, path and command, so it runs a line as it is. A command is kept as the session typed
it, so a value typed into a command is in the log. Off unless the policy names it in `[guards] on`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl
from ws.guards import Finding, known_keys
from ws.profile import ws_home

NAME = "record-calls"
EVENTS = ("pre",)
KINDS = ("command", "read", "search", "write", "tool", "agent", "skill", "message")

LOG = "gate-calls.jsonl"
_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}  # lower case: Claude Code's names and OpenCode's
_TEXT_KEYS = {"content", "old_string", "new_string", "new_source", "oldString", "newString", "newSource"}


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def log_path(home: Path | None = None) -> Path:
    return (home if home is not None else ws_home()) / "logs" / LOG


def _fingerprint(text: str) -> dict:
    data = text.encode("utf-8", "replace")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _redact_text(fields: dict) -> dict:
    out = {k: _fingerprint(v) if k in _TEXT_KEYS and isinstance(v, str) else v for k, v in fields.items()}
    if isinstance(out.get("edits"), list):  # MultiEdit: a list of {old_string, new_string}
        out["edits"] = [_redact_text(e) if isinstance(e, dict) else e for e in out["edits"]]
    return out


def redact(payload: dict) -> dict:
    """The payload with the text of a file write replaced by its size and fingerprint (see the module docstring)."""
    tool = payload.get("tool_name", payload.get("tool"))
    if not isinstance(tool, str) or tool.lower() not in _WRITE_TOOLS:
        return payload
    out = dict(payload)
    for key in ("tool_input", "args"):
        if isinstance(out.get(key), dict):
            out[key] = _redact_text(out[key])
    return out


def judge(call, cfg, ctx) -> Finding | None:
    if ctx.memo.get(NAME):
        return None  # one line per hook run, however many calls the payload held: replay reads the payload back whole
    ctx.memo[NAME] = True
    stamp = datetime.fromtimestamp(ctx.now(), UTC).isoformat(timespec="seconds")
    try:
        jsonl.append(log_path(ctx.home), {"at": stamp, "runner": ctx.runner, "payload": redact(ctx.payload)})
    except OSError as e:  # recording is a convenience: a log that cannot be written never stops the call
        return Finding("warn", f"record-calls could not write {LOG} ({type(e).__name__})")
    return None
