"""`ws session`: tell a long-running session once when ws has been updated under it.

A session loads its rules and skills when it starts. If ws is released while it runs, it keeps working from the old
ones. `ws session start` records the session and `ws session nudge` prints one line, once, when the installed release
is newer than the one it began on. The record is one JSON file per session in `<state>/sessions/<id>.json`, written
atomically; docs/reference/stores.md lists it. It is also the session registry other modules read.

Registry v2 (`SCHEMA_VERSION` 2) keeps every v1 key (`schema`, `ts`, `session_id`, `release`, `started_at`, `runner`,
`cwd`: the home page reads them) and adds `pane` (`%N` from the hook's own `TMUX_PANE`, else None), `role` (lead, worker
or reviewer; worker by default), `class` (the data class of `cwd`, ws.policy.class_of; None when the policy has no
classes or cannot load, and a reader treats None as the strictest), `state` (busy, idle or None) with `state_ts`,
`last_delivered_outbound` ({id, ts} or None) and `rev` (a whole number, bumped on every write). Other modules use the
setters, not the file:

  * `read(session_id, env=None) -> Record | None`: a v1 record reads with the defaults (role worker, class None, rev 0);
    a missing, unreadable or corrupt record reads as None, which a reader must treat as the strictest case.
  * `update(session_id, *, state=..., last_delivered_outbound=..., expect_rev=None)`: under a lock, change those two
    fields (a field left out is kept). With `expect_rev`, a record changed since it was read is refused with a
    `WsError` of kind `conflict`, so "the registry changed between check and act" is detectable. It cannot set `role`
    or `class`.
  * `prepare_launch(role) -> token`: the launcher's side of how a role is set. The role comes only from a single-use
    token (`<state>/sessions/launch/<token>`, mode 0600, good for `LAUNCH_TTL` seconds) written by a process outside the
    session before the runner starts: `ws session prepare --role R` prints `WS_LAUNCH=<token>` for the launcher to
    export, and `ws session start` claims that token from its own environment and deletes it. An expired, reused,
    missing or forged token gives a worker, and a resumed or compacted start for the same id keeps the role it had.
    `ws session prepare` and `start` are refused from a gated session's shell (ws.policy), so a worker cannot mint a
    lead or overwrite another record; the hooks that run `start` are not tool calls.

Who is calling. A shell command has no way to say which session it belongs to, and an environment variable is the
command's own to set, so identity comes from the process tree. `ws session start` is a hook: it stamps the record with
`runner_pid` and `runner_start`, the runner's process id and the time that process started (the nearest ancestor of the
hook that is not a shell or a plain launcher such as `env` or `uv`; the start time stops a reused id from matching).
`resolve_caller(env=None) -> Record | None` walks this process's ancestors, nearest first, and returns the record
carrying the first stamp that matches (the newest, when a cleared or resumed session left two for one runner). It works
unchanged for `ws mcp serve`, which is a child of the runner. A process with no matching ancestor (orphaned, or
double-forked so that init owns it) is not a session and gets None. Only `start` writes the stamp: `update` keeps it and
cannot set it. The ancestors come from `/proc` on Linux and `ps` on macOS. The keys are additions, so the schema stays 2.

Records are pruned 30 days after their last change (`_KEEP_DAYS`); a data class's `retention_days` is the message
store's own, not this.

`start` and `nudge` are advice, not a gate. The gate (`ws gate check`) fails closed; these do the opposite: any error
exits 0 and says nothing, so a broken nudge can never block or slow the host tool. `prepare` is a launcher command and
reports its errors.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__, jsonl, output, paths
from ws.output import WsError

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RELEASE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")  # a release string is printed into the model's context: keep it plain
_KEEP_DAYS = 30
SCHEMA_VERSION = 2  # the `schema` of a session record in sessions/<id>.json
ROLES = ("lead", "worker", "reviewer")
STATES = ("busy", "idle")
LAUNCH_ENV = "WS_LAUNCH"  # what the launcher exports and `ws session start` claims
LAUNCH_TTL = 600  # seconds a launch token is good for: shared by `prepare` (writes it) and `start` (claims it)
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_PANE = re.compile(r"^%[0-9]{1,9}$")
_UNSET = ...  # `update` leaves a field alone when it is not given (None is a value: it clears the field)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "mksh", "ash", "csh", "tcsh", "busybox"})
_LAUNCHERS = frozenset({"env", "uv", "uvx", "nohup", "nice", "timeout", "setsid"})  # pass a command on; not the runner
_MAX_DEPTH = 64  # ancestors looked at: a process tree is never this deep, and a loop must not hang the walk


# --- the release ---------------------------------------------------------------------------------------------------


def _marker(env: dict) -> str:
    """A release marker if one is configured: $WS_RELEASE (the id itself), or a path in $WS_RELEASE_FILE, or
    `release` in ws's config folder. A path may be a file holding the id, or a folder (or link to one) named by the id, which is how
    older ws lays out ~/.ws-release/<id>."""
    if env.get("WS_RELEASE", "").strip():
        return _plain(env["WS_RELEASE"].strip())
    path = (
        Path(env["WS_RELEASE_FILE"]).expanduser() if env.get("WS_RELEASE_FILE") else paths.config_dir(env) / "release"
    )
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


def safe_id(session_id: str) -> str:
    """A session id as a file name: anything odd is hashed rather than trusted."""
    return session_id if _SAFE_ID.match(session_id) else hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _record_path(env: dict, session_id: str) -> Path:
    return paths.state_dir(env) / "sessions" / f"{safe_id(session_id)}.json"


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
    """Drop records not touched for 30 days (a session that old is long gone). A guard's own record for a session
    (`<id>.<name>.json`, see ws.guards) goes with it."""
    cutoff = time.time() - _KEEP_DAYS * 86400
    for f in folder.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def _read(path: Path) -> dict | None:
    """The record at `path` as a dict, or None when it is missing, unreadable, not an object or has no plain release."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("release"), str) and _RELEASE.match(data["release"])
    return data if ok else None


def _now() -> float:
    return time.time()


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Hold the registry's lock: one writer at a time, across threads and processes. POSIX `flock` on a file beside the
    sessions folder (so the folder holds only records), imported here so the module still loads where there is no `fcntl`
    (native Windows runs only the doctor)."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    fd = os.open(folder.parent / ".sessions.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _rev(data: dict | None) -> int:
    rev = data.get("rev") if data else None
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0


def _pane(env: dict) -> str | None:
    pane = env.get("TMUX_PANE", "")
    return pane if _PANE.fullmatch(pane) else None


def _class_name(cwd: str) -> str | None:
    """The name of the data class of `cwd`; None when the policy has no classes or cannot be loaded (a reader treats None
    as the strictest case)."""
    try:
        from ws import policy

        found = policy.class_of(policy.load(), cwd)
    except Exception:  # noqa: BLE001 - a policy that does not load must not stop a session record
        return None
    return found.name if found else None


def _new_record(
    session_id: str,
    release: str,
    runner: str,
    cwd: str,
    env: dict,
    previous: dict | None = None,
    stamp: Proc | None = None,
) -> dict:
    """A record for a session starting now. The role, the delivery cursor and the revision carry over from `previous` (a
    resumed, cleared or compacted start for the same id); a new role is only ever set by `start`, from the launch token.
    `stamp` is the runner process (`start` passes it; a resumed start stamps the new runner)."""
    now = datetime.now(UTC)
    before = Record.from_dict(previous, session_id) if previous else None
    out = {
        "schema": SCHEMA_VERSION,
        "ts": jsonl.iso_utc(now.timestamp()),
        "session_id": session_id,
        "release": release,
        "started_at": now.isoformat(timespec="seconds"),
        "runner": runner,
        "cwd": cwd,
        "pane": _pane(env),
        "role": before.role if before else "worker",
        "class": _class_name(cwd),
        "state": None,
        "state_ts": None,
        "last_delivered_outbound": before.last_delivered_outbound if before else None,
        "rev": _rev(previous) + 1,
    }
    if stamp:
        out["runner_pid"], out["runner_start"] = stamp.pid, stamp.start
    return out


# --- who is calling: the process tree ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    """A process as the walk sees it: `start` is a string that is the same for the life of the process and differs for a
    process that later reuses the id (clock ticks since boot on Linux, `ps` start time elsewhere)."""

    pid: int
    start: str
    ppid: int
    name: str


def proc_info(pid: int) -> Proc | None:
    """The process `pid`, or None when it is gone or cannot be read. `/proc` on Linux, `ps` where there is none."""
    try:
        if Path("/proc/self/stat").exists():
            data = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
            name = data[data.index("(") + 1 : data.rindex(")")]
            rest = data[data.rindex(")") + 2 :].split()  # state, ppid, ... starttime is the 20th after the name
            return Proc(pid, rest[19], int(rest[1]), name)
    except (OSError, ValueError, IndexError):
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
            check=False,
        ).stdout
        parts = out.split(None, 6)  # ppid, then lstart as five words, then the command (which may hold spaces)
        if len(parts) < 7:
            return None
        return Proc(pid, " ".join(parts[1:6]), int(parts[0]), os.path.basename(parts[6].strip()))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _ancestors() -> list[Proc]:
    """This process's ancestors, nearest first (its parent, then that process's parent, and so on to the top)."""
    out: list[Proc] = []
    seen = {os.getpid()}
    pid = os.getppid()
    while pid > 0 and pid not in seen and len(out) < _MAX_DEPTH:
        seen.add(pid)
        proc = proc_info(pid)
        if proc is None:
            break
        out.append(proc)
        pid = proc.ppid
    return out


def _is_runner(proc: Proc) -> bool:
    return proc.name.lstrip("-").lower() not in _SHELLS | _LAUNCHERS


def runner_process() -> Proc | None:
    """The runner behind this hook: the nearest ancestor that is not a shell or a plain launcher. None when there is none."""
    return next((p for p in _ancestors() if _is_runner(p)), None)


@dataclass(frozen=True)
class Record:
    """A session as the registry knows it. `class_` is the JSON key `class`: the data class name, or None (not known:
    treat as the strictest). A field the file lacks or holds in a shape it should not takes its default."""

    session_id: str
    release: str
    ts: str | None = None
    started_at: str | None = None
    runner: str = "unknown"
    cwd: str = ""
    pane: str | None = None
    role: str = "worker"
    class_: str | None = None
    state: str | None = None
    state_ts: str | None = None
    last_delivered_outbound: dict | None = None
    rev: int = 0
    nudged: bool = False
    schema: int = 1
    runner_pid: int | None = None  # the runner process `start` stamped (with runner_start); None on an older record
    runner_start: str | None = None

    @classmethod
    def from_dict(cls, data: dict, session_id: str = "") -> Record:
        def text(key: str) -> str | None:
            v = data.get(key)
            return v if isinstance(v, str) else None

        sent = data.get("last_delivered_outbound")
        sent_ok = isinstance(sent, dict) and isinstance(sent.get("id"), str) and isinstance(sent.get("ts"), str)
        schema = data.get("schema")
        pane = data.get("pane")
        pid, began = data.get("runner_pid"), text("runner_start")
        stamped = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and bool(began) and len(began) <= 64
        return cls(
            session_id=text("session_id") or session_id,
            release=text("release") or "",
            ts=text("ts"),
            started_at=text("started_at"),
            runner=text("runner") or "unknown",
            cwd=text("cwd") or "",
            pane=pane if isinstance(pane, str) and _PANE.fullmatch(pane) else None,
            role=data["role"] if data.get("role") in ROLES else "worker",
            class_=text("class"),
            state=data["state"] if data.get("state") in STATES else None,
            state_ts=text("state_ts"),
            last_delivered_outbound={"id": sent["id"], "ts": sent["ts"]} if sent_ok else None,
            rev=_rev(data),
            nudged=data.get("nudged") is True,
            schema=schema if isinstance(schema, int) and not isinstance(schema, bool) else 1,
            runner_pid=pid if stamped else None,
            runner_start=began if stamped else None,
        )

    def to_dict(self) -> dict:
        out = {
            "schema": SCHEMA_VERSION,
            "ts": self.ts,
            "session_id": self.session_id,
            "release": self.release,
            "started_at": self.started_at,
            "runner": self.runner,
            "cwd": self.cwd,
            "pane": self.pane,
            "role": self.role,
            "class": self.class_,
            "state": self.state,
            "state_ts": self.state_ts,
            "last_delivered_outbound": self.last_delivered_outbound,
            "rev": self.rev,
        }
        if self.nudged:
            out["nudged"] = True
        if self.runner_pid is not None and self.runner_start is not None:
            out["runner_pid"], out["runner_start"] = self.runner_pid, self.runner_start
        return out


def _env(env: dict | None) -> dict:
    return paths.merged_environ() if env is None else env


def read(session_id: str, env: dict | None = None) -> Record | None:
    """The record of `session_id`, or None when there is none, it cannot be read or it is corrupt. A v1 record reads with
    the v2 defaults (role worker, class None, rev 0). A reader treats None as the strictest case."""
    data = _read(_record_path(_env(env), session_id))
    return Record.from_dict(data, session_id) if data is not None else None


def resolve_caller(env: dict | None = None) -> Record | None:
    """The session this process belongs to, or None. Walks the ancestors of this process, nearest first, and returns the
    record whose runner stamp (`runner_pid` and `runner_start`) is that ancestor; where a cleared or resumed session left
    more than one record for the same runner process, the newest. Nothing the process was given (an environment variable,
    an argument, a payload) takes part, so a command cannot name itself as another session. A process that is not under a
    stamped runner (orphaned, or double-forked and handed to init) is not a session: None. A records folder that cannot be
    read is None too."""
    chain = _ancestors()
    if not chain:
        return None
    folder = paths.state_dir(_env(env)) / "sessions"
    found: dict[tuple[int, str], list[Record]] = {}
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return None
    for f in files:
        data = _read(f)
        rec = Record.from_dict(data) if data is not None else None
        if rec and rec.session_id and rec.runner_pid is not None and rec.runner_start is not None:
            found.setdefault((rec.runner_pid, rec.runner_start), []).append(rec)
    for proc in chain:
        hits = found.get((proc.pid, proc.start))
        if hits:
            return max(hits, key=lambda r: (r.started_at or "", r.ts or "", r.session_id))
    return None


def update(
    session_id: str,
    *,
    state: str | None = _UNSET,
    last_delivered_outbound: dict | None = _UNSET,
    expect_rev: int | None = None,
    env: dict | None = None,
) -> Record:
    """Change `state` (busy, idle or None; `state_ts` is set with it) and/or `last_delivered_outbound` ({id, ts} or
    None) on a record, under the registry lock, and return the record as written. A field left out is kept. The record's
    `rev` goes up by one. With `expect_rev`, a record whose rev is not that is refused (`WsError`, kind `conflict`) and
    nothing is written: read, check, then update with the rev you read. No record (or an unreadable one) is `not_found`.
    There is deliberately no way here to set `role` or `class`."""
    if state is not _UNSET and state not in (*STATES, None):
        raise WsError("bad_input", f"state must be busy, idle or None, not {state!r}", "pass one of those", 2)
    if last_delivered_outbound is not _UNSET and last_delivered_outbound is not None:
        sent = last_delivered_outbound
        if not (isinstance(sent, dict) and isinstance(sent.get("id"), str) and isinstance(sent.get("ts"), str)):
            raise WsError("bad_input", "last_delivered_outbound must be {id, ts} (text) or None", "pass both keys", 2)
        last_delivered_outbound = {"id": sent["id"], "ts": sent["ts"]}
    path = _record_path(_env(env), session_id)
    with _locked(path.parent):
        data = _read(path)
        if data is None:
            raise WsError("not_found", f"no readable session record for {session_id!r}", "start the session first")
        rec = Record.from_dict(data, session_id)
        if expect_rev is not None and rec.rev != expect_rev:
            raise WsError(
                "conflict",
                f"the session record changed (rev {rec.rev}, expected {expect_rev})",
                "read it again and decide again",
            )
        if state is _UNSET and last_delivered_outbound is _UNSET:
            return rec
        now = jsonl.iso_utc(_now())
        changes: dict = {"rev": rec.rev + 1, "ts": now}
        if state is not _UNSET:
            changes.update(state=state, state_ts=now)
        if last_delivered_outbound is not _UNSET:
            changes["last_delivered_outbound"] = last_delivered_outbound
        new = replace(rec, **changes)
        _write(path, new.to_dict())
        return new


# --- launch tokens: how a role gets set ----------------------------------------------------------------------------


def _launch_dir(env: dict) -> Path:
    return paths.state_dir(env) / "sessions" / "launch"


def prepare_launch(role: str, env: dict | None = None) -> str:
    """The launcher's side: write a single-use token for a session that will start with `role` and return it. The caller
    exports it as `WS_LAUNCH` before it starts the runner. The file is private (0600), names the role and the time, and
    is good for `LAUNCH_TTL` seconds; older leftovers are cleared here."""
    if role not in ROLES:
        raise WsError(
            "usage", f"role must be one of {', '.join(ROLES)}, not {role!r}", "pass lead, worker or reviewer", 2
        )
    folder = _launch_dir(_env(env))
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    now = _now()
    for old in folder.iterdir():  # a token nobody claimed in time, or the leftover of a claim that died half way
        with contextlib.suppress(OSError):
            if now - old.lstat().st_mtime > LAUNCH_TTL * 2:
                old.unlink()
    token = secrets.token_hex(16)
    fd = os.open(folder / token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "created": now}) + "\n")
    return token


def _claim_launch(env: dict, token: str | None) -> str | None:
    """The role a launch token carries, or None. The token is taken (renamed away, then deleted) whether or not it is
    good, so it works once: a second start with it, a forged name, a token past its time, one that is not a plain file of
    ours or one that is not private all give None, which `start` reads as worker."""
    if not token or not _TOKEN.fullmatch(token):
        return None
    folder = _launch_dir(env)
    taken = folder / f".claimed.{token}.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        os.rename(folder / token, taken)  # one winner when two starts race
    except OSError:
        return None
    try:
        st = taken.lstat()
        uid = getattr(os, "geteuid", lambda: st.st_uid)()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
            return None
        data = json.loads(taken.read_text(encoding="utf-8"))
        created = data.get("created") if isinstance(data, dict) else None
        role = data.get("role") if isinstance(data, dict) else None
        if isinstance(created, bool) or not isinstance(created, int | float) or role not in ROLES:
            return None
        return role if 0 <= _now() - created <= LAUNCH_TTL else None
    except (OSError, ValueError):
        return None
    finally:
        taken.unlink(missing_ok=True)


def _input(args: argparse.Namespace) -> tuple[str, str] | None:
    """(session id, cwd) from --session-id, else from the runner's hook JSON on stdin. None if there is no id. Nothing
    else in the payload is used: a role or class it carries is ignored."""
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
    try:
        with paths.pinned_by(args.pin):
            got = _input(args)
            if got:
                env = paths.merged_environ()
                role = _claim_launch(env, env.get(LAUNCH_ENV))  # taken even if it is no good: it works once
                # always overwrite: a resumed, cleared or compacted session reloads its rules, so it starts again here
                path = _record_path(env, got[0])
                with _locked(path.parent):
                    new = _new_record(
                        got[0], current_release(env), args.runner, got[1], env, _read(path), runner_process()
                    )
                    if role:
                        new["role"] = role
                    _write(path, new)
                    _prune(path.parent)
    except WsError as e:  # ws's folders are not where this hook was installed to find them: say so, do nothing
        _skipped(e)
    except Exception:  # noqa: BLE001 - advice, not a gate: never fail the host tool
        return 0
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    token = prepare_launch(args.role)
    if args.json:
        output.result({LAUNCH_ENV: token, "role": args.role, "ttl_seconds": LAUNCH_TTL})
    else:
        print(f"{LAUNCH_ENV}={token}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    try:
        with paths.pinned_by(args.pin):
            _nudge(args)
    except WsError as e:
        _skipped(e)
    except Exception:  # noqa: BLE001 - advice, not a gate: never fail the host tool
        return 0
    return 0


def _skipped(e: WsError) -> None:
    with contextlib.suppress(Exception):
        print(f"ws session: skipped ({e.message}; {e.hint})", file=sys.stderr)


def _nudge(args: argparse.Namespace) -> None:
    got = _input(args)
    if not got:
        return
    env = paths.merged_environ()
    sid, cwd = got
    path = _record_path(env, sid)
    now = current_release(env)
    with _locked(path.parent):
        rec = _read(path)
        if rec is None:  # none yet, or one we cannot read: record now, say nothing
            _write(path, _new_record(sid, now, args.runner, cwd, env))
            return
        if rec.get("nudged") or not is_newer(now, rec["release"]):
            return
        # mark first: if this fails we stay quiet rather than repeat ourselves
        _write(path, {**rec, "nudged": True, "rev": _rev(rec) + 1, "ts": jsonl.iso_utc(_now())})
        recorded = rec["release"]
    print(
        f"ws {now} is out (this session started on {recorded}): run checkpoint and restart at the next phase boundary."
    )


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
        paths.add_pin_option(p)
        p.set_defaults(func=fn, raw=True)  # hook entries: the runner reads their stdout
    p = ss.add_parser("prepare", help="launcher: print WS_LAUNCH=<token>, a one-use token that gives a session a role")
    p.add_argument("--role", required=True, choices=ROLES, help="the role the session will have")
    p.set_defaults(func=cmd_prepare)
