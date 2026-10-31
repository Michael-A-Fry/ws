"""`ws session`: tell a long-running session once when ws has been updated under it.

A session loads its rules and skills when it starts. If ws is released while it runs, it keeps working from the old
ones. `ws session start` records the release a session began on; `ws session nudge` prints one line, once, when the
installed release is newer than that.

This is advice, not a gate. The gate (`ws gate check`) fails closed; this does the opposite: any error exits 0 and
says nothing, so a broken nudge can never block or slow the host tool.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__
from ws.profile import ws_home

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RELEASE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")  # a release string is printed into the model's context: keep it plain
_KEEP_DAYS = 30


# --- the release ---------------------------------------------------------------------------------------------------


def _marker(env: dict) -> str:
    """A release marker if one is configured: $WS_RELEASE (the id itself), or a path in $WS_RELEASE_FILE, or
    $WS_HOME/release. A path may be a file holding the id, or a folder (or link to one) named by the id, which is how
    older ws lays out ~/.ws-release/<id>."""
    if env.get("WS_RELEASE", "").strip():
        return _plain(env["WS_RELEASE"].strip())
    path = Path(env["WS_RELEASE_FILE"]).expanduser() if env.get("WS_RELEASE_FILE") else ws_home(env) / "release"
    try:
        if path.is_dir():
            return _plain(path.resolve().name)
        if path.is_file():
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            return _plain(lines[0].strip()) if lines else ""
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _plain(marker: str) -> str:
    """The marker if it is a plain release string, else nothing (it is dropped, not repaired)."""
    return marker if _RELEASE.match(marker) else ""


def current_release(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    marker = _marker(env)
    release = f"{__version__}+{marker}" if marker else __version__
    return release if _RELEASE.match(release) else (__version__ if _RELEASE.match(__version__) else "unknown")


def _natural(text: str) -> list[tuple[int, int | str]]:
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", text) if p]


def release_key(release: str) -> tuple:
    version, _, marker = release.partition("+")
    return (_natural(version), _natural(marker))


def is_newer(current: str, recorded: str) -> bool:
    return release_key(current) > release_key(recorded)


# --- records -------------------------------------------------------------------------------------------------------


def _record_path(env: dict, session_id: str) -> Path:
    # a session id becomes a file name: anything odd is hashed rather than trusted
    sid = session_id if _SAFE_ID.match(session_id) else hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return ws_home(env) / "sessions" / f"{sid}.json"


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # a new, uniquely named file that refuses to follow a planted link, then an atomic rename over the record
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _prune(folder: Path) -> None:
    """Drop records not touched for 30 days (a session that old is long gone)."""
    cutoff = time.time() - _KEEP_DAYS * 86400
    for f in folder.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("release"), str) and _RELEASE.match(data["release"])
    return data if ok else None


def _new_record(session_id: str, release: str, runner: str, cwd: str) -> dict:
    return {
        "session_id": session_id,
        "release": release,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runner": runner,
        "cwd": cwd,
    }


def _input(args: argparse.Namespace) -> tuple[str, str] | None:
    """(session id, cwd) from --session-id, else from the runner's hook JSON on stdin. None if there is no id."""
    cwd = os.getcwd()
    sid = args.session_id
    if not sid and not sys.stdin.isatty():
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            sid = payload.get("session_id")
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
    return (sid, cwd) if isinstance(sid, str) and sid else None


# --- commands ------------------------------------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    with contextlib.suppress(Exception):  # advice, not a gate: never fail the host tool
        got = _input(args)
        if got:
            env = dict(os.environ)
            # always overwrite: a resumed, cleared or compacted session reloads its rules, so it starts again here
            path = _record_path(env, got[0])
            _write(path, _new_record(got[0], current_release(env), args.runner, got[1]))
            _prune(path.parent)
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    with contextlib.suppress(Exception):  # advice, not a gate: never fail the host tool
        got = _input(args)
        if not got:
            return 0
        env = dict(os.environ)
        sid, cwd = got
        path = _record_path(env, sid)
        now = current_release(env)
        rec = _read(path)
        if rec is None:  # none yet, or one we cannot read: record now, say nothing
            _write(path, _new_record(sid, now, args.runner, cwd))
            return 0
        if rec.get("nudged") or not is_newer(now, rec["release"]):
            return 0
        rec["nudged"] = True
        _write(path, rec)  # mark first: if this fails we stay quiet rather than repeat ourselves
        print(
            f"ws {now} is out (this session started on {rec['release']}): "
            "run checkpoint and restart at the next phase boundary."
        )
    return 0


def register(sub) -> None:
    s = sub.add_parser("session", help="record the release a session started on; nudge once if ws has moved on")
    ss = s.add_subparsers(dest="session_command", required=True)
    for name, fn, text in (
        ("start", cmd_start, "hook entry: record this session's release (hook JSON on stdin, or --session-id)"),
        ("nudge", cmd_nudge, "hook entry: print one line, once, if ws is newer than this session's release"),
    ):
        p = ss.add_parser(name, help=text)
        p.add_argument("--session-id", metavar="ID", help="the session id (default: session_id in the hook JSON)")
        p.add_argument("--runner", default="unknown", help="which runner is calling (recorded)")
        p.set_defaults(func=fn, raw=True)  # hook entries: the runner reads their stdout
