"""Bound, single-use approvals: how a call the policy marks `ask_commands` gets a human's yes, once.

    gate (hook)   a call matches ask_commands -> a pending record, and the call is refused with an id
    ws approve ID a person, in a terminal of their own, grants it -> a granted record
    gate (hook)   the identical call is retried -> the granted record is consumed (deleted) and the call runs

What an approval is bound to: sha256 over (runner, kind, exact command or path, cwd, session id). It is the id and the
file name. A different command, another folder, another session or another runner hashes to a different id and so
needs its own approval; one changed byte is a new call. It is single use (the granted file is claimed by an atomic
rename, so two racing calls cannot both use it) and it expires: a pending one 10 minutes after the request, a granted
one 10 minutes after the grant.

Where it lives: `$WS_HOME/approvals/pending/<id>.json` (written by the gate) and `.../granted/<id>.json` (written only
by `ws approve`). `approvals` is a ws control path (ws.policy.CONTROL_NAMES), so a session's own write tools cannot
touch it and the sandbox keeps `granted/` read-only. `pending/` is the one writable exception in the OpenCode wall
(ws.sandbox): the gate runs inside that wall and must be able to ask. That is safe because a pending record grants
nothing: `ws approve` re-derives the id from the record's own fields before it grants, and shows the person the exact
text, so a forged pending record can only put a call in front of the person that they can refuse. Files are 0600 in
0700 folders, so under the secret wall (ws-secrets) the agent's user cannot write them at all.

Inside the OpenCode wall the hook cannot consume a grant: `granted/` is read-only there (making it writable would let a
session replay or pre-plant grants). So `ws gate launch` keeps a socketpair, outside the wall, and hands the runner the
other end as an inherited descriptor (WS_APPROVAL_FD). The hook only *peeks* at the record and exits 4 with the id; the
shim sends `claim <id>` down the descriptor; the launcher, outside the wall, consumes the record (the same atomic rename
as above) and answers `ok` or `no <why>`. An inherited descriptor survives the wall's block on creating new Unix
sockets. A forged claim from inside can only burn an approval, never create one: the shim decides what runs.

What the person grants is exactly what was shown: `ws approve` resolves the typed prefix once, to one record, shows
that record, and writes the grant from the same parsed fields, never re-reading the folder (which the wall leaves
writable at `pending/`). A planted record with the same prefix makes the prefix ambiguous; it does not swap the call.

Who may approve: `ws approve` refuses when stdin is not a terminal, and when the session's env marker is set
(WS_GATED_SESSION, put there by the generated Claude Code settings and by `ws gate launch`). The matcher also denies
`ws approve` as a shell command. A session that allocates its own pty and unsets the marker defeats the first two; the
third and the sandbox are what stand then, and this is not a defence against a determined session that can run
arbitrary programs outside the wall. It stops the model approving itself by accident or by a plain instruction.

Tamper: a granted record whose fields no longer hash to its id, whose timestamps are out of order or in the future, or
that is unreadable, is refused and removed; the call goes back to asking. That catches a damaged or edited file. It does
not stop a writer who can compute the hash, which is why the folder is a control path.

Why not custodian-claude-guard (PyPI, MIT, v0.1.0 of 2 Aug 2026, alpha, 0 stars): it was read and not adopted. It is a
Claude Code PreToolUse hook only (Codex through a shared engine), so it cannot guard OpenCode or any later runner, and
ws is runner-neutral. It brings its own policy engine and a `custodian-kernel` dependency beside ours, where ws src is
stdlib only and has one policy file. Its approval step is Claude Code's own permission dialog, which does not exist in
headless `claude -p` or in OpenCode, so there is no out-of-band human step for a timer or a job. Its tamper-proof mode
needs root and a managed-settings file. It is a fair design (bound, single use); the thin version here is the same idea
over the files ws already protects.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ws.profile import ws_home

TTL = 600  # seconds a pending or a granted approval lives
ENV_MARKER = "WS_GATED_SESSION"  # set in a gated session's env; `ws approve` refuses when it is there
_SHORT = 12  # hex digits of the id shown to a person
_ID = re.compile(r"^[0-9a-f]{8,64}$")
_FULL = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
FD_ENV = "WS_APPROVAL_FD"  # the inherited claim socket, set by `ws gate launch` inside the OpenCode wall
_SKEW = 60  # seconds a record's clock may run ahead before it counts as tampered


class ApprovalError(Exception):
    pass


@dataclass(frozen=True)
class Bound:
    """What an approval is for."""

    runner: str
    kind: str
    value: str
    cwd: str
    session: str

    def digest(self) -> str:
        blob = json.dumps([self.runner, self.kind, self.value, self.cwd, self.session], ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def approvals_dir(home: Path | None = None) -> Path:
    return (home if home is not None else ws_home()) / "approvals"


def _pending(root: Path) -> Path:
    return root / "pending"


def _granted(root: Path) -> Path:
    return root / "granted"


# --- files ---------------------------------------------------------------------------------------------------------


def _write(path: Path, record: dict) -> None:
    """A new file that refuses to follow a planted link, then an atomic rename over the record."""
    for d in (path.parent.parent, path.parent):
        d.mkdir(mode=0o700, exist_ok=True)
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _record(bound: Bound, created: float, granted: float | None = None) -> dict:
    rec = {"v": 1, "id": bound.digest(), "bound": bound.__dict__.copy(), "created": created}
    if granted is not None:
        rec["granted"] = granted
    return rec


def _parse(text: str, ident: str, now: float) -> tuple[Bound, dict]:
    """The bound fields and the record in `text`, or ApprovalError: unreadable, edited (the fields no longer hash to
    `ident`, the id the file is named by), or with timestamps that cannot be true."""
    try:
        rec = json.loads(text)
        b = rec["bound"]
        bound = Bound(**{k: b[k] for k in ("runner", "kind", "value", "cwd", "session")})
        times = [rec["created"], *([rec["granted"]] if "granted" in rec else [])]
    except (ValueError, KeyError, TypeError) as e:
        raise ApprovalError(f"unreadable ({type(e).__name__})") from e
    if not all(isinstance(v, str) for v in bound.__dict__.values()):
        raise ApprovalError("bound fields that are not text")
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in times):
        raise ApprovalError("a timestamp that is not a number")
    if bound.digest() != ident or rec.get("id") != ident:
        raise ApprovalError("changed: its fields no longer match its id")
    if times != sorted(times) or times[-1] > now + _SKEW:
        raise ApprovalError("timestamps that are out of order or in the future")
    return bound, rec


def _read_regular(path: Path, limit: int = 65536) -> str:
    """A file's text (at most `limit` bytes; more is an error). Opened without following a link and without blocking, and
    refused unless it is a regular file: `pending/` is writable from the wall, and a FIFO there would hang the reader."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ApprovalError(f"unreadable ({type(e).__name__})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ApprovalError("not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
            text = f.read(limit + 1)
        if len(text) > limit:
            raise ApprovalError("too large")
        return text
    except (OSError, UnicodeDecodeError) as e:
        raise ApprovalError(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(fd)


def _load(path: Path, now: float) -> tuple[Bound, dict]:
    return _parse(_read_regular(path), path.stem, now)


def _expired(rec: dict, now: float) -> bool:
    return now > rec.get("granted", rec["created"]) + TTL


def _prune(root: Path, now: float) -> None:
    """Drop expired and unreadable records, and stray temp files (best effort)."""
    for folder in (_pending(root), _granted(root)):
        for f in folder.glob("*"):
            with contextlib.suppress(OSError):
                try:
                    stale = f.suffix != ".json" or _expired(_load(f, now)[1], now)
                except ApprovalError:
                    stale = True
                if stale and (f.suffix == ".json" or now - f.stat().st_mtime > TTL):
                    f.unlink()


# --- the gate's side -----------------------------------------------------------------------------------------------


_NONE = "no approval"


@dataclass(frozen=True)
class Claim:
    """A granted approval exists for this call but could not be consumed here: the caller must `claim` it."""

    ident: str


def consume_id(root: Path, ident: str, now: float) -> str | None:
    """Use up the granted approval with this id. None = used; else why not (_NONE: there was none)."""
    path = _granted(root) / f"{ident}.json"
    claimed = path.with_name(f".{ident}.{os.getpid()}.{secrets.token_hex(4)}.used")
    try:
        os.rename(path, claimed)  # atomic: of two racing calls, one wins and the other finds nothing
    except FileNotFoundError:
        return _NONE
    except OSError as e:
        return f"approval could not be claimed: {type(e).__name__}"
    try:
        text = _read_regular(claimed)
    except ApprovalError as e:
        text = f"{e}"
    finally:
        claimed.unlink(missing_ok=True)
    return _verdict(text, ident, now)


def _verdict(text: str, ident: str, now: float) -> str | None:
    try:
        _, rec = _parse(text, ident, now)
    except ApprovalError as e:
        return f"the approval was {e} and is discarded"
    if "granted" not in rec:
        return "the approval was never granted and is discarded"
    if _expired(rec, now):
        return "the approval expired"
    return None


def peek(root: Path, ident: str, now: float) -> str | None:
    """Like consume_id, but leaves the record: None = a valid granted approval is there."""
    try:
        text = _read_regular(_granted(root) / f"{ident}.json")
    except ApprovalError:
        return _NONE if not (_granted(root) / f"{ident}.json").exists() else "the approval is unreadable"
    return _verdict(text, ident, now)


def require(root: Path, bound: Bound, now: float | None = None, consume: bool = True) -> str | Claim | None:
    """None if this exact call holds a granted approval (which is used up). Else the reason it is refused, with the
    id to approve: a pending record is written (or kept, if one is still fresh). With consume=False (inside the
    wall, where the grant cannot be consumed) a valid grant gives a Claim for the caller to send to the launcher."""
    now = time.time() if now is None else now
    root = Path(root)
    ident = bound.digest()
    why = consume_id(root, ident, now) if consume else peek(root, ident, now)
    if why is None:
        return None if consume else Claim(ident)
    path = _pending(root) / f"{ident}.json"
    fresh = False
    with contextlib.suppress(ApprovalError, OSError):
        fresh = not _expired(_load(path, now)[1], now)
    if not fresh:
        _write(path, _record(bound, now))
    with contextlib.suppress(OSError):
        _prune(root, now)
    note = "" if why == _NONE else f" ({why})"
    return (
        f"needs a person's approval{note}: in a terminal of your own run `ws approve {ident[:_SHORT]}`, "
        f"then retry the identical call (good for one use, within {TTL // 60} minutes)"
    )


def serve_claims(sock: socket.socket, root: Path) -> None:
    """The launcher's side of the claim socket, outside the wall: `claim <64 hex> <nonce>` -> `ok <nonce>` or
    `no <nonce> <why>`. The nonce is the runner's, fresh per request, so it can tell its own answer from a stale one.
    Runs until the runner closes its end."""
    with sock, sock.makefile("rw", encoding="utf-8", newline="\n") as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "claim" and _FULL.match(parts[1]) and _NONCE.match(parts[2]):
                why = consume_id(Path(root), parts[1], time.time())
                f.write(f"ok {parts[2]}\n" if why is None else f"no {parts[2]} {why}\n")
            else:
                f.write("no - bad request\n")
            f.flush()


def claim(sock: socket.socket, ident: str) -> str | None:
    """The runner's side (used by tests; the OpenCode shim does the same in JavaScript). None = granted and used."""
    nonce = secrets.token_hex(8)
    f = sock.makefile("rw", encoding="utf-8", newline="\n")
    f.write(f"claim {ident} {nonce}\n")
    f.flush()
    answer = f.readline().strip()
    if answer == f"ok {nonce}":
        return None
    return answer.removeprefix(f"no {nonce} ") if answer.startswith(f"no {nonce}") else answer or "no answer"


# --- the person's side ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    path: Path
    bound: Bound
    rec: dict


def _find(root: Path, prefix: str, now: float) -> Found:
    """The one pending record whose id starts with `prefix`. Resolved once: everything after uses this result."""
    prefix = prefix.lower()
    if not _ID.match(prefix):
        raise ApprovalError("an id is 8 to 64 hex digits, as `ws approve --list` shows")
    hits = sorted(_pending(root).glob(f"{prefix}*.json")) if _pending(root).is_dir() else []
    if not hits:
        raise ApprovalError(f"no pending approval starts with {prefix} (it may have expired: retry the call)")
    if len(hits) > 1:
        raise ApprovalError(f"{prefix} matches {len(hits)} pending approvals: give more digits")
    bound, rec = _load(hits[0], now)
    if _expired(rec, now):
        raise ApprovalError(f"{prefix} expired: retry the call to get a new id")
    return Found(hits[0], bound, rec)


def pending(root: Path, now: float | None = None) -> list[tuple[str, Bound, float]]:
    """(id, bound, seconds left) for each live pending record, oldest first."""
    now = time.time() if now is None else now
    out = []
    for f in sorted(_pending(Path(root)).glob("*.json")) if _pending(Path(root)).is_dir() else []:
        with contextlib.suppress(ApprovalError):
            bound, rec = _load(f, now)
            if not _expired(rec, now):
                out.append((rec["id"], bound, rec["created"] + TTL - now))
    return sorted(out, key=lambda t: t[2])


def grant_found(root: Path, found: Found, now: float | None = None) -> str:
    """Grant exactly the call in `found` (what the person was shown), whatever the folder holds now."""
    now = time.time() if now is None else now
    ident = found.bound.digest()
    _write(_granted(root) / f"{ident}.json", _record(found.bound, found.rec["created"], now))
    found.path.unlink(missing_ok=True)
    return ident


def grant(root: Path, prefix: str, now: float | None = None) -> tuple[str, Bound]:
    """Find, then grant. Raises ApprovalError. A person's terminal shows `bound` between the two (see cmd_approve)."""
    now = time.time() if now is None else now
    found = _find(Path(root), prefix, now)
    return grant_found(Path(root), found, now), found.bound


def describe(bound: Bound) -> str:
    """The call as a person should see it. JSON-escaped: the text came from a model, and a raw control or escape
    sequence could repaint the terminal to show something else."""
    j = json.dumps
    return (
        f"  id      {bound.digest()}\n  runner  {j(bound.runner)}\n  kind    {j(bound.kind)}\n"
        f"  call    {j(bound.value)}\n  cwd     {j(bound.cwd)}\n  session {j(bound.session)}"
    )


# --- ws approve ----------------------------------------------------------------------------------------------------


def refuse_reason(env: dict, stdin_is_tty: bool) -> str | None:
    if env.get(ENV_MARKER):
        return f"ws approve is for a person, not for a gated session ({ENV_MARKER} is set here)"
    if not stdin_is_tty:
        return "ws approve needs a terminal (stdin is not one): run it yourself, not from a session or a script"
    return None


def cmd_approve(args: argparse.Namespace) -> int:
    root = approvals_dir()
    if args.list:
        if os.environ.get(ENV_MARKER):
            print(f"ws: {refuse_reason(os.environ, True)}", file=sys.stderr)
            return 1
        rows = pending(root)
        for ident, bound, left in rows:
            print(
                f"{ident[:_SHORT]}  {int(left // 60)}m left  {json.dumps(bound.runner)}  {json.dumps(bound.value)[:100]}"
            )
        if not rows:
            print("ws: nothing is waiting for approval")
        return 0
    why = refuse_reason(os.environ, sys.stdin.isatty())
    if why:
        print(f"ws: {why}", file=sys.stderr)
        return 1
    if not args.id:
        print("ws: give the id the gate printed, or --list", file=sys.stderr)
        return 1
    try:
        found = _find(root, args.id, time.time())
        print("Approve this call, once?\n" + describe(found.bound))
        if not args.yes and input("approve? [y/N] ").strip().lower() not in ("y", "yes"):
            print("ws: not approved")
            return 1
        ident = grant_found(root, found)
    except (ApprovalError, EOFError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(f"ws: approved {ident[:_SHORT]} for one use, within {TTL // 60} minutes; the session may retry the call")
    return 0


def register(sub) -> None:
    p = sub.add_parser("approve", help="a person approves one pending call the gate refused (single use, 10 minutes)")
    p.add_argument("id", nargs="?", help="the id the gate printed (8 or more hex digits)")
    p.add_argument("--list", action="store_true", help="show what is waiting for approval")
    p.add_argument("--yes", action="store_true", help="do not ask to confirm (still needs a terminal)")
    p.set_defaults(func=cmd_approve)
