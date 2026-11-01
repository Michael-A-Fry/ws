"""Claude Code's own session state: the live list from `claude agents --json`, joined with its per-process files.

`claude agents --json` prints a list of live sessions ({sessionId, cwd, name, pid, status, waitingFor, startedAt}). Each
live process also leaves `<claude dir>/sessions/<pid>.json` ({sessionId, tmux, name, status, statusUpdatedAt}); those
files add the tmux target and when the status last changed. The program runs through `env.run` with a timeout, so a
missing binary, a timeout, a bad exit or text that isn't JSON is one `sources[]` row. A session waiting for input is
also an ask of kind `waiting`. Only `*.json` files are listed, so the `*.key` files beside them are never opened.
"""

from __future__ import annotations

import json

from ws.home import records
from ws.home.records import Ask, Session
from ws.home.sources import BAD_FORMAT, FAILED, TIMEOUT, UNAVAILABLE, SourceError, _files

NAME = "claude_code"
PROVIDES = ("sessions", "asks")
ORDER = 10
LIVE_RUNNER = "claude-code"  # a session of this runner that this source didn't list is no longer live
TIMEOUT_S = 10.0
ARGV = ["claude", "agents", "--json"]

_STATE = {
    "busy": "working",
    "working": "working",
    "running": "working",
    "active": "working",
    "idle": "idle",
    "waiting": "waiting",
    "blocked": "waiting",
    "ended": "ended",
    "exited": "ended",
    "stopped": "ended",
    "done": "ended",
}
_WAIT_KEYS = ("text", "prompt", "message", "title", "reason", "kind", "type")


def available(env):
    return True, ""


def _waiting_text(raw) -> str:
    """What a session is waiting for, as one short line; `input` when the value gives no words."""
    if isinstance(raw, str):
        return _files.text(raw, 200)
    if isinstance(raw, dict):
        for key in _WAIT_KEYS:
            if isinstance(raw.get(key), str) and raw[key].strip():
                return _files.text(raw[key], 200)
    return "input" if raw else ""


def _live(env) -> list[dict]:
    try:
        code, out = env.run(ARGV, TIMEOUT_S)
    except FileNotFoundError as e:
        raise SourceError(UNAVAILABLE, "claude isn't installed here") from e
    except TimeoutError as e:
        raise SourceError(TIMEOUT, f"claude agents took longer than {TIMEOUT_S:g} s") from e
    except OSError as e:
        raise SourceError(UNAVAILABLE, f"can't run claude: {e.strerror or type(e).__name__}") from e
    if code != 0:
        raise SourceError(FAILED, f"claude agents exited with {code}")
    try:
        data = json.loads(out)
    except (ValueError, RecursionError) as e:
        raise SourceError(BAD_FORMAT, "claude agents --json didn't print JSON") from e
    if isinstance(data, dict):
        data = data.get("agents", data.get("sessions"))
    if not isinstance(data, list) or not all(isinstance(a, dict) for a in data):
        raise SourceError(BAD_FORMAT, "claude agents --json didn't print a list of sessions")
    return data


def _pid_files(env) -> dict[str, dict]:
    """The per-process files by session id."""
    out: dict[str, dict] = {}
    for path in _files.json_files(env.path("claude_dir") / "sessions"):
        rec = _files.read_object(path)
        if rec and isinstance(rec.get("sessionId"), str):
            out[rec["sessionId"]] = rec
    return out


def _both(env):
    if "claude_code" not in env.cache:
        env.cache["claude_code"] = (_live(env), _pid_files(env))
    return env.cache["claude_code"]


def read(kind, env):
    live, files = _both(env)
    sessions, asks = [], []
    for agent in live:
        sid = agent.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        extra = files.get(sid, {})
        cwd = _files.text(agent.get("cwd") or extra.get("cwd"), 500)
        waiting = _waiting_text(agent.get("waitingFor"))
        state = "waiting" if waiting else _STATE.get(str(agent.get("status") or extra.get("status")).lower(), "unknown")
        name = _files.text(agent.get("name") or extra.get("name"), 80)
        tmux = _files.text(extra.get("tmux"), 80)
        if kind == "sessions":
            sessions.append(
                Session(
                    id=sid,
                    source=NAME,
                    runner=LIVE_RUNNER,
                    name=name,
                    tmux=tmux,
                    cwd=cwd,
                    repo=_files.repo_label(cwd),
                    state=state,
                    waiting_for=waiting,
                    started_at=_files.iso(agent.get("startedAt")),
                    last_seen=_files.iso(env.now),
                )
            )
        elif waiting:
            asks.append(
                Ask(
                    id=f"waiting:{sid}",
                    source=NAME,
                    kind="waiting",
                    session_id=sid,
                    session_name=name,
                    tmux=tmux,
                    repo=_files.repo_label(cwd),
                    title=waiting,
                    created=_files.iso(extra.get("statusUpdatedAt") or extra.get("updatedAt")),
                    opaque=records.under(cwd, env.cfg.roots),
                )
            )
    return sessions if kind == "sessions" else asks
