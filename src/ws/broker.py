"""The secrets broker: a small process that is not an LLM and holds the store, so agents never see a value.

How a secret reaches a tool without the tool reading it:

1. The runner asks the broker over a Unix socket: `request(name, purpose, host)`. The broker checks the caller, the
   registry (which hosts and purposes the name is allowed for) and that a value is set. Then it returns a short-lived
   lease: a base URL on 127.0.0.1 plus the header name the tool should use with any placeholder key.
2. The runner points the tool's base URL at the lease (Claude Code and OpenCode both honour one).
3. The lease proxy strips any auth header the tool sent, adds the real one, and forwards only to https://<host> for
   that lease. Responses stream back as they arrive. A lease is for one host, expires (1 h by default) and can be
   released.

No TLS interception and no CA: the tool talks plain HTTP to 127.0.0.1 and the broker talks TLS to the real host. Stdlib
only. Everything fails closed: an unset name, a missing or loose store, an unknown host or an expired lease gives a
refusal, never a request without the credential or to a different host.

Wire format on the socket: one JSON object per line, one reply per request. Ops: request, release, status, elevate,
and for the minters (ws.minters) mint, sign and ca_public.
Refusal codes: unknown_name, unset, host_not_allowed, purpose_not_allowed, caller_refused, store_unreadable, expired,
run_required, run_refused, not_declared, area_refused, tier_refused, claim_refused, elevation_off, audit_failed,
wrong_kind, upstream_refused, upstream_unreachable, plus bad_request (malformed) and busy (too many connections or
leases).

Run records, the trust anchor for scoped names. A request names only its run id (`run`). The broker reads its own run
record for that id: one JSON file per run in `<state>/run/records/`, owned by the broker's user (or root), mode 0600 in
a 0700 folder, holding the run's area, tier and declared names (`ws broker run-record` writes one; the launcher will).
Anything the request claims beyond the id (an area, a tier) must match the record or the request is refused, so a
caller cannot talk its way up. A scoped registry name (one with a `tier` or `area`) is leased only when the record
declares it, its area matches and the run's tier reaches it. The lease ends with the run: the proxy re-reads the record
on every request, and a removed or expired record ends every lease issued under it.

Every grant and every refusal is one audit line (never a value); a grant whose audit line cannot be written is refused.
`ws elevate off` is the kill switch: the broker refuses every elevated (`lead-elevated`) name at once, drops the live
leases for them and asks each revoker (a minter that can revoke its upstream token) to revoke what it issued. It is
persisted in `<state>/elevate.json`; a file that cannot be read or trusted counts as off.

Callers: SO_PEERCRED on Linux, LOCAL_PEERCRED on macOS. Only the broker's own uid and members of `--group` are served.
The lease proxy is a loopback port, so the lease token is the credential: it goes only to an allowed caller.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
import socket
import socketserver
import ssl
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ws import __version__, output, paths
from ws import secrets as wsecrets
from ws.minters import github_app, ssh_ca
from ws.output import WsError

CODES = (
    "unknown_name",
    "unset",
    "host_not_allowed",
    "purpose_not_allowed",
    "caller_refused",
    "store_unreadable",
    "expired",
    "bad_request",
    "busy",
    "run_required",
    "run_refused",
    "not_declared",
    "area_refused",
    "tier_refused",
    "claim_refused",
    "elevation_off",
    "audit_failed",
    "wrong_kind",
    "upstream_refused",
    "upstream_unreachable",
)
MINT_REUSE_MARGIN_S = 300  # a cached token with less left than this is minted afresh
REAP_EVERY_S = 30.0  # how often the broker looks for runs that ended with tokens still live
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RECORD_KEYS = {"v", "run", "area", "tier", "names", "uid", "expires_at", "repos", "permissions"}
MAX_RECORD = 65536
DEFAULT_TTL_S = 3600
MAX_CONNECTIONS = 16
MAX_LEASES = 256
CONN_DEADLINE_S = 30.0
MAX_LINE = 65536
MAX_BODY = 64 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 300
MAX_PROXY_CONNECTIONS = 64
REQUEST_DEADLINE_S = 120.0  # headers and body of one proxied request, however slowly they trickle in
MAX_REJECTING = 32

# headers that can carry a credential: dropped from the tool's request before the real one is added
STRIP_REQUEST = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-goog-api-key",
    "cookie",
}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "expect",
}
CHUNK_SIZE_RE = re.compile(rb"^[0-9a-fA-F]{1,8}$")
TOKEN_RE = re.compile(r"^/l/([A-Za-z0-9_-]{43})(/.*)?$")


class BrokerError(Exception):
    """Never carries a secret value or a lease token."""


class BrokerUnavailable(BrokerError):
    """The broker is not running, or this user cannot reach its socket. `not_running` is True only when the socket file
    is missing or nothing is listening on it (a stale file): the one case where starting a broker can help. A timeout, a
    permission error or a bad reply leaves it False."""

    def __init__(self, message: str, not_running: bool = False):
        super().__init__(message)
        self.not_running = not_running


class BrokerRefused(BrokerError):
    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


# The longest Unix socket path the system takes, in bytes: sun_path is 104 bytes on macOS and the BSDs, 108 on Linux,
# and one is the NUL. Past it connect() and bind() fail with a plain OSError, which says nothing typed about why.
UNIX_PATH_MAX = 104 if sys.platform != "linux" else 108


class SocketPathTooLong(BrokerUnavailable):
    """The broker socket's path is longer than this system's Unix sockets allow. Raised before connect or bind, so it
    is its own case rather than a guess from an OSError's text. Starting a broker would not help (`not_running` is
    False)."""

    def __init__(self, path: str | Path):
        n = len(os.fsencode(str(path)))
        super().__init__(
            f"the broker socket path is {n} bytes; this system's limit is {UNIX_PATH_MAX - 1}",
            not_running=False,
        )
        self.hint = (
            "use a shorter WS_HOME (or XDG_STATE_HOME), or set WS_BROKER_SOCKET (or [roles.secrets] socket) to a short path "
            "such as /tmp/ws-broker.sock"
        )


def check_socket_path(path: str | Path) -> None:
    """Raises SocketPathTooLong if a Unix socket cannot have this path."""
    if len(os.fsencode(str(path))) >= UNIX_PATH_MAX:
        raise SocketPathTooLong(path)


class RecordError(BrokerError):
    """A run record that is missing, loose, malformed or over. `kind`: missing | untrusted | malformed | ended."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class RunRecord:
    """What the broker believes about one run. Written by the launcher (or `ws broker run-record`), never by a request."""

    run: str
    area: str
    tier: str
    names: tuple[str, ...]
    uid: int | None = None  # when set, only this caller uid may lease under the run
    expires_at: float | None = None  # when set, the run (and every lease under it) is over at this time
    repos: tuple[str, ...] = ()  # the repositories a minted GitHub token may cover; none means no token
    permissions: tuple[tuple[str, str], ...] = ()  # the most a minted token may hold, as (name, level)

    def to_json(self) -> dict:
        d: dict = {"v": 1, "run": self.run, "area": self.area, "tier": self.tier, "names": list(self.names)}
        if self.uid is not None:
            d["uid"] = self.uid
        if self.expires_at is not None:
            d["expires_at"] = _iso(self.expires_at)
        if self.repos:
            d["repos"] = list(self.repos)
        if self.permissions:
            d["permissions"] = dict(self.permissions)
        return d


def records_dir(env: dict | None = None) -> Path:
    """Where the broker reads run records: `run/records/` in the broker's own state folder."""
    return paths.state_dir(env) / "run" / "records"


def _check_private(st: os.stat_result, own_uid: int, what: str, directory: bool) -> None:
    if st.st_uid not in (own_uid, 0):
        raise RecordError("untrusted", f"{what} must belong to the broker's user or root")
    if st.st_mode & 0o077:
        want = "0700" if directory else "0600"
        raise RecordError("untrusted", f"{what} is open to other users (mode {st.st_mode & 0o777:04o}, want {want})")


def _parse_record(raw: object, run_id: str) -> RunRecord:
    if not isinstance(raw, dict) or raw.get("v") != 1 or set(raw) - RECORD_KEYS:
        raise RecordError("malformed", "the run record is not a version 1 record")
    run, area, tier, names = raw.get("run"), raw.get("area"), raw.get("tier"), raw.get("names")
    if run != run_id:
        raise RecordError("malformed", "the run record is for another run")
    if not (isinstance(area, str) and wsecrets.AREA_RE.match(area)):
        raise RecordError("malformed", "the run record's area is not an area name")
    if tier not in wsecrets.TIERS:
        raise RecordError("malformed", "the run record's tier is not a tier")
    if not (isinstance(names, list) and all(isinstance(n, str) and wsecrets.NAME_RE.match(n) for n in names)):
        raise RecordError("malformed", "the run record's names must be a list of secret names")
    uid = raw.get("uid")
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or uid < 0):
        raise RecordError("malformed", "the run record's uid must be a number")
    exp = raw.get("expires_at")
    expires: float | None = None
    if exp is not None:
        try:
            when = datetime.fromisoformat(str(exp))
        except ValueError as e:
            raise RecordError("malformed", "the run record's expires_at is not an ISO 8601 time") from e
        if when.tzinfo is None:
            raise RecordError("malformed", "the run record's expires_at must carry a time zone (Z for UTC)")
        expires = when.timestamp()
    repos = raw.get("repos", [])
    if not (isinstance(repos, list) and all(isinstance(r, str) and github_app.REPO_RE.match(r) for r in repos)):
        raise RecordError("malformed", "the run record's repos must be a list of repository names")
    perms = raw.get("permissions", {})
    if not (
        isinstance(perms, dict)
        and all(
            isinstance(k, str) and github_app.PERMISSION_RE.match(k) and v in github_app.LEVELS
            for k, v in perms.items()
        )
    ):
        raise RecordError("malformed", "the run record's permissions must map names to read, write or admin")
    return RunRecord(
        run=run,
        area=area,
        tier=tier,
        names=tuple(names),
        uid=uid,
        expires_at=expires,
        repos=tuple(repos),
        permissions=tuple(sorted(perms.items())),
    )


def read_run_record(folder: Path, run_id: object, own_uid: int, now: float | None = None) -> RunRecord:
    """The run record for `run_id`, checked: the folder and the file belong to `own_uid` or root and are closed to
    others, the file is a regular file reached without a symlink, and it parses as a version 1 record for this run.
    Raises RecordError."""
    if not (isinstance(run_id, str) and RUN_ID_RE.match(run_id)):
        raise RecordError("malformed", "the run id is not a run id (letters, digits, '.', '-' and '_')")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
    except FileNotFoundError as e:
        raise RecordError("missing", "there are no run records") from e
    except OSError as e:
        raise RecordError("untrusted", "the run record folder cannot be opened (a symlink is refused)") from e
    try:
        dst = os.fstat(dfd)
        if not stat.S_ISDIR(dst.st_mode):
            raise RecordError("untrusted", "the run record folder is not a folder")
        _check_private(dst, own_uid, "the run record folder", True)
        try:
            fd = os.open(run_id, os.O_RDONLY | nofollow, dir_fd=dfd)  # the checked folder, not a path a swap can move
        except FileNotFoundError as e:
            raise RecordError("missing", "no run record for that run") from e
        except OSError as e:
            raise RecordError("untrusted", "the run record cannot be opened (a symlink is refused)") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RecordError("untrusted", "the run record is not a regular file")
            _check_private(st, own_uid, "the run record", False)
            data = os.read(fd, MAX_RECORD + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    if len(data) > MAX_RECORD:
        raise RecordError("malformed", "the run record is too big")
    try:
        raw = json.loads(data)
    except ValueError as e:
        raise RecordError("malformed", "the run record is not JSON") from e
    rec = _parse_record(raw, run_id)
    if rec.expires_at is not None and rec.expires_at <= (time.time() if now is None else now):
        raise RecordError("ended", "the run is over")
    return rec


def write_run_record(folder: Path, rec: RunRecord) -> Path:
    """Write a run record atomically, mode 0600 in a 0700 folder (made if missing). The folder must be the writer's."""
    if not RUN_ID_RE.match(rec.run):
        raise RecordError("malformed", "the run id is not a run id")
    _parse_record(json.loads(json.dumps(rec.to_json())), rec.run)  # the writer refuses what the reader would
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = folder.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise RecordError("untrusted", f"{folder} must be yours and mode 0700")
    tmp = folder / f".{rec.run}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec.to_json(), fh)
            fh.write("\n")
        os.replace(tmp, folder / rec.run)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return folder / rec.run


def remove_run_record(folder: Path, run_id: str) -> bool:
    """End a run: its record goes, and with it every lease issued under it."""
    if not RUN_ID_RE.match(run_id):
        raise RecordError("malformed", "the run id is not a run id")
    try:
        (folder / run_id).unlink()
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------- the kill switch


def elevate_path(env: dict | None = None) -> Path:
    return paths.state_dir(env) / "elevate.json"


def read_elevation(path: Path, own_uid: int) -> bool:
    """True when elevated names may be leased. No file: on (nothing has pulled the switch). A file that cannot be read,
    does not parse, or that another user could have written: off, because a kill switch fails towards off."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (own_uid, 0) or st.st_mode & 0o022:
            return False
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    try:
        raw = json.loads(data)
    except ValueError:
        return False
    return isinstance(raw, dict) and raw.get("elevated") == "on"


def write_elevation(path: Path, on: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"elevated": "on" if on else "off", "t": _iso(time.time())}, fh)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@dataclass(frozen=True)
class Lease:
    lease: str  # an id for release and the audit log; not a credential
    base_url: str  # point the tool here; the path after it is passed to the allowed host
    header: str  # the header the tool sends its placeholder key in
    expires_at: str  # ISO 8601, UTC


# ---------------------------------------------------------------- client


def _client_call(msg: dict, socket_file: str | Path | None, timeout: float = 10.0) -> dict:
    path = str(socket_file) if socket_file else str(wsecrets.socket_path())
    check_socket_path(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise BrokerUnavailable("the broker's reply was too long")
        reply = json.loads(buf.split(b"\n", 1)[0])
    except (OSError, ValueError) as e:
        down = isinstance(e, (FileNotFoundError, ConnectionRefusedError))
        raise BrokerUnavailable(f"no broker answering at {path} ({type(e).__name__})", not_running=down) from e
    finally:
        s.close()
    if not isinstance(reply, dict):
        raise BrokerUnavailable("the broker's reply was not a JSON object")
    if reply.get("ok") is not True:
        raise BrokerRefused(str(reply.get("code", "bad_request")), str(reply.get("reason", "")))
    return reply


def request(
    name: str,
    purpose: str,
    host: str,
    socket: str | Path | None = None,
    ttl_s: float | None = None,
    run: str | None = None,
) -> Lease:
    """Ask for a lease, under run id `run` when given. Raises BrokerRefused(code, reason) or BrokerUnavailable."""
    msg: dict = {"op": "request", "name": name, "purpose": purpose, "host": host}
    if ttl_s is not None:
        msg["ttl_s"] = ttl_s
    if run is not None:
        msg["run"] = run
    r = _client_call(msg, socket)["lease"]
    return Lease(lease=r["lease"], base_url=r["base_url"], header=r["header"], expires_at=r["expires_at"])


def release(lease: Lease | str, socket: str | Path | None = None) -> None:
    """End a lease early. Raises BrokerRefused("expired", ...) if it is already gone."""
    _client_call({"op": "release", "lease": lease.lease if isinstance(lease, Lease) else lease}, socket)


def elevate(on: bool, socket: str | Path | None = None) -> dict:
    """Move the kill switch on a running broker: {"elevated": bool, "dropped": n, "revoked": n, "revoke_failed": n}."""
    r = _client_call({"op": "elevate", "state": "on" if on else "off"}, socket)
    return {k: v for k, v in r.items() if k != "ok"}


def status(socket: str | Path | None = None, timeout: float = 3.0) -> dict:
    """The broker's view of itself: version, port, lease count and store health. No values."""
    r = _client_call({"op": "status"}, socket, timeout)
    return {k: v for k, v in r.items() if k != "ok"}


# ---------------------------------------------------------------- who is calling


def peer_cred(conn: socket.socket) -> tuple[int, int]:
    """(pid, uid) of the process on the other end of a Unix socket. Raises BrokerError if this OS cannot say."""
    if sys.platform.startswith("linux"):
        pid, uid, _gid = struct.unpack(
            "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        return pid, uid
    if (
        sys.platform == "darwin"
    ):  # LOCAL_PEERCRED returns a struct xucred; LOCAL_PEERPID the pid (what getpeereid reads)
        sol_local, local_peercred, local_peerpid = 0, 0x001, 0x002
        _ver, uid = struct.unpack_from("II", conn.getsockopt(sol_local, local_peercred, 76))
        (pid,) = struct.unpack("i", conn.getsockopt(sol_local, local_peerpid, 4))
        return pid, uid
    raise BrokerError("this OS cannot say who is on a Unix socket")


def in_group(uid: int, group: str | None) -> bool:
    if not group:
        return False
    import grp  # POSIX only: imported here so `ws doctor` still runs on native Windows (and says to use WSL2)
    import pwd

    try:
        g = grp.getgrnam(group)
        pw = pwd.getpwuid(uid)
    except KeyError:
        return False
    return pw.pw_gid == g.gr_gid or pw.pw_name in g.gr_mem


# ---------------------------------------------------------------- audit


class Audit:
    """One JSON line per op: time, op, name, purpose, host, caller uid/pid, lease id prefix, run, area, tier, status,
    bytes. Never a value or a lease token. Free text from callers is shortened and name/host/run must look like what
    they are. `write` says whether the line reached the file, so a grant can be refused when it did not."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, op: str, status: str, **f) -> bool:
        if self.path is None:
            return True
        name, host, run = f.get("name"), f.get("host"), f.get("run")
        rec = {
            "t": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "op": op,
            "name": name if isinstance(name, str) and wsecrets.NAME_RE.match(name) else (None if name is None else "?"),
            "purpose": str(f["purpose"])[:120] if f.get("purpose") is not None else None,
            "host": host if isinstance(host, str) and wsecrets.HOST_RE.match(host) else (None if host is None else "?"),
            "uid": f.get("uid"),
            "pid": f.get("pid"),
            "lease": (f.get("lease") or "")[:6] or None,
            "run": run if isinstance(run, str) and RUN_ID_RE.match(run) else (None if run is None else "?"),
            "area": f.get("area") if isinstance(f.get("area"), str) and wsecrets.AREA_RE.match(f["area"]) else None,
            "tier": f.get("tier") if f.get("tier") in wsecrets.TIERS else None,
            "status": status,
            "bytes": f.get("bytes"),
        }
        line = (json.dumps(rec, ensure_ascii=True) + "\n").encode()
        try:
            with self._lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    if os.write(fd, line) != len(line):
                        raise OSError("short write")
                finally:
                    os.close(fd)
        except OSError:
            print("ws broker: cannot write the audit log", file=sys.stderr)
            return False
        return True


# ---------------------------------------------------------------- the broker


@dataclass
class _Grant:
    id: str
    name: str
    host: str
    purpose: str
    header: str
    expires: float
    uid: int
    pid: int
    run: str | None = None
    area: str | None = None
    tier: str | None = None
    elevated: bool = False


@dataclass
class _Minted:
    name: str
    run: str
    elevated: bool
    token: str
    options: object
    expires: float

    def __repr__(self) -> str:  # never the token
        return f"_Minted({self.name}, {self.run})"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class _HttpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


class Broker:
    def __init__(
        self,
        socket_path: Path,
        registry: Path,
        store: wsecrets.Store,
        group: str | None = None,
        log_path: Path | None = None,
        max_ttl_s: float = DEFAULT_TTL_S,
        port: int = 0,
        clock=time.time,
        allow_plain_http: bool = False,
        records: Path | None = None,
        elevate_file: Path | None = None,
        require_run: bool = False,
    ):
        self.socket_path = Path(socket_path)
        self.registry = registry
        self.store = store
        self.group = group
        self.audit = Audit(log_path)
        self.max_ttl_s = max_ttl_s
        self.clock = clock
        self.allow_plain_http = allow_plain_http  # tests only: http to 127.0.0.1; the command line has no switch for it
        self.own_uid = os.geteuid()
        self._port = port
        self._grants: dict[str, _Grant] = {}  # sha256(token) -> grant
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._unix: socketserver.BaseServer | None = None
        self._http: ThreadingHTTPServer | None = None
        self._stopped = threading.Event()
        self.records = records  # the run record folder; None means none, so every scoped name is refused
        self.elevate_file = elevate_file  # the kill switch's file; None means elevation is never switched off
        self.require_run = require_run  # every lease needs a run record, scoped name or not
        self._forced_off = False  # `elevate off` held in memory, so a switch file that cannot be written still bites
        # Called with each live grant `ws elevate off` drops; a minter that can revoke its upstream token adds one. One
        # that raises counts as a failed revoke and is logged; the grant is dropped either way.
        self.revokers: list = []
        self._minted: list[_Minted] = []  # tokens a minter issued, kept until they expire, to revoke on `elevate off`
        self._mint_cache: dict[tuple, tuple[str, float]] = {}  # (name, run, repos, perms) -> (token, expires)

    @classmethod
    def from_env(
        cls,
        socket_file: str | None,
        group: str | None,
        port: int,
        ttl: float,
        env: dict | None = None,
        allow_plain_http: bool = False,
        require_run: bool = False,
    ):
        env = os.environ if env is None else env
        # --socket wins; else what clients use: $WS_BROKER_SOCKET, [roles.secrets] socket, <state>/run/broker.sock
        sock = Path(socket_file).expanduser() if socket_file else wsecrets.socket_path(env)
        return cls(
            sock,
            wsecrets.registry_path(env),
            wsecrets.Store(wsecrets.store_dir(env)),
            group=group,
            log_path=paths.state_dir(env) / "logs" / "broker.jsonl",
            max_ttl_s=ttl,
            port=port,
            allow_plain_http=allow_plain_http,
            records=records_dir(env),
            elevate_file=elevate_path(env),
            require_run=require_run,
        )

    # -- lifecycle

    @property
    def port(self) -> int:
        assert self._http is not None
        return self._http.server_address[1]

    def start(self) -> Broker:
        # the loopback port first: the socket file appearing is the signal that the broker is ready to answer
        self._http = _ProxyServer(("127.0.0.1", self._port), _ProxyHandler, self)
        threading.Thread(target=self._http.serve_forever, args=(0.05,), daemon=True, name="ws-broker-proxy").start()
        try:
            self._bind_unix()
        except BaseException:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
            raise
        assert self._unix is not None
        threading.Thread(target=self._unix.serve_forever, args=(0.05,), daemon=True, name="ws-broker-socket").start()
        self._stopped.clear()
        threading.Thread(target=self._reaper, daemon=True, name="ws-broker-reaper").start()
        return self

    def stop(self) -> None:
        for srv in (self._unix, self._http):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        self._unix = self._http = None
        self._stopped.set()

    def serve_forever(self) -> None:
        self.start()
        self._stopped.wait()

    def request_stop(self) -> None:
        threading.Thread(target=self.stop, daemon=True).start()

    def _bind_unix(self) -> None:
        check_socket_path(self.socket_path)
        d = self.socket_path.parent
        gid = None
        if self.group:
            import grp

            try:
                gid = grp.getgrnam(self.group).gr_gid
            except KeyError as e:
                raise BrokerError(f"no such group: {self.group}") from e
        want = 0o750 if self.group else 0o700
        if not d.exists():
            d.mkdir(mode=want, parents=True)
            d.chmod(want)  # past the umask
        st = d.stat()
        # a folder that was already there is never chmod-ed (it might be a home folder): it must already be right
        if st.st_uid != self.own_uid:
            raise BrokerError(f"{d} must belong to the broker's user, or someone else could swap the socket")
        if gid is not None and st.st_gid != gid:
            os.chown(d, -1, gid)
        if self.group and st.st_mode & 0o050 != 0o050:
            raise BrokerError(f"{d} must be readable and enterable by group {self.group} (mode 0750)")
        if st.st_mode & (0o027 if self.group else 0o077):
            raise BrokerError(
                f"{d} can be changed or entered by other users; use a private folder (0700, or 0750 with a group)"
            )
        self._clear_stale_socket()
        old = os.umask(0o117 if self.group else 0o177)
        try:
            self._unix = _unix_server(self)
        finally:
            os.umask(old)
        self.socket_path.chmod(0o660 if self.group else 0o600)
        if gid is not None:
            try:
                os.chown(self.socket_path, -1, gid)
            except PermissionError as e:
                self._unix.server_close()
                raise BrokerError(
                    f"cannot give group {self.group} the socket: add the broker's user to that group"
                ) from e

    def _clear_stale_socket(self) -> None:
        p = self.socket_path
        try:
            st = p.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise BrokerError(f"{p} exists and is not a socket")
        try:
            status(p, timeout=1.0)
        except BrokerRefused as e:
            raise BrokerError(f"a broker is already running at {p}") from e
        except BrokerUnavailable:
            p.unlink()
        else:
            raise BrokerError(f"a broker is already running at {p}")

    # -- connections

    def caller_allowed(self, uid: int) -> bool:
        return uid == self.own_uid or in_group(uid, self.group)

    def handle_conn(self, conn: socket.socket) -> None:
        deadline = time.monotonic() + CONN_DEADLINE_S
        uid = pid = -1
        try:
            pid, uid = peer_cred(conn)
            allowed = self.caller_allowed(uid)
        except (BrokerError, OSError):
            allowed = False
        buf = b""
        while True:
            line, buf = _read_line(conn, buf, deadline)
            if line is None:
                return
            if not allowed:
                self.audit.write("request", "caller_refused", uid=uid, pid=pid)
                reply = _refuse("caller_refused", "this user may not use the broker")
            else:
                reply = self.dispatch(line, uid, pid)
            conn.sendall(json.dumps(reply).encode() + b"\n")
            if not allowed:
                return

    def dispatch(self, line: bytes, uid: int, pid: int) -> dict:
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            return _refuse("bad_request", "not a JSON object")
        op = msg.get("op")
        try:
            if op == "request":
                return self._op_request(msg, uid, pid)
            if op == "release":
                return self._op_release(msg, uid, pid)
            if op == "status":
                return self._op_status()
            if op == "elevate":
                return self._op_elevate(msg, uid, pid)
            if op == github_app.OP:
                return self._op_mint(msg, uid, pid)
            if op == ssh_ca.OP:
                return self._op_sign(msg, uid, pid)
            if op == "ca_public":
                return self._op_ca_public(msg)
        except Exception as e:  # noqa: BLE001 (answer, never crash the thread; the type only, in case a value is near)
            print(f"ws broker: {op} failed ({type(e).__name__})", file=sys.stderr)
            return _refuse("store_unreadable", "the broker could not complete that")
        return _refuse("bad_request", "unknown op")

    def _op_request(self, msg: dict, uid: int, pid: int) -> dict:
        name, purpose, host, ttl = msg.get("name"), msg.get("purpose", ""), msg.get("host"), msg.get("ttl_s")
        run_id = msg.get("run")
        log = {"name": name, "purpose": purpose, "host": host, "uid": uid, "pid": pid, "run": run_id}

        def refuse(code: str, reason: str) -> dict:
            self.audit.write("request", code, **log)
            return _refuse(code, reason)

        if not (isinstance(name, str) and isinstance(purpose, str) and isinstance(host, str)):
            return refuse("bad_request", "name, purpose and host must be strings")
        host = host.lower()
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not ttl > 0):
            return refuse("bad_request", "ttl_s must be a number above 0")
        if not wsecrets.NAME_RE.match(name) or not wsecrets.HOST_RE.match(host) or len(purpose) > 200:
            return refuse("bad_request", "bad name, host or purpose")
        try:
            reg = wsecrets.load_registry(self.registry)
        except wsecrets.SecretsError:
            return refuse("store_unreadable", "the registry cannot be read")
        entry = reg.get(name)
        if entry is None:
            return refuse("unknown_name", f"{name} is not in the registry")
        if host not in entry.hosts:
            return refuse("host_not_allowed", f"{name} is not allowed at {host}")
        if entry.kind != "header":
            return refuse("wrong_kind", f"{name} is a {entry.kind} entry: ask with the {entry.kind} op, not a lease")
        if entry.purposes and purpose not in entry.purposes:
            return refuse("purpose_not_allowed", f"{name} is not allowed for that purpose")
        now = self.clock()
        rec, why = self._scope(entry, msg, uid, log, now, need_run=False)
        if why:
            return refuse(*why)
        try:
            self.store.read(name)
        except FileNotFoundError:
            return refuse("unset", f"{name} has no value; run `ws secret set {name}`")
        except wsecrets.SecretsError as e:
            return refuse("store_unreadable", str(e))
        token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        expires = now + min(float(ttl), self.max_ttl_s) if ttl is not None else now + self.max_ttl_s
        if rec is not None and rec.expires_at is not None:
            expires = min(expires, rec.expires_at)  # a lease never outlives its run
        grant = _Grant(
            id=os.urandom(6).hex(),
            name=name,
            host=host,
            purpose=purpose,
            header=entry.header,
            expires=expires,
            uid=uid,
            pid=pid,
            run=run_id if rec is not None else None,
            area=rec.area if rec is not None else None,
            tier=rec.tier if rec is not None else None,
            elevated=entry.elevated,
        )
        with self._lock:
            self._purge(now)
            if len(self._grants) >= MAX_LEASES:
                return refuse("busy", "too many open leases; release some")
            self._grants[_key(token)] = grant
        if not self.audit.write("request", "ok", lease=grant.id, **log):
            with self._lock:
                self._grants.pop(_key(token), None)
            return _refuse("audit_failed", "the grant could not be written to the audit log, so it is not given")
        return {
            "ok": True,
            "lease": {
                "lease": grant.id,
                "base_url": f"http://127.0.0.1:{self.port}/l/{token}",
                "header": grant.header,
                "expires_at": _iso(grant.expires),
            },
        }

    def _op_release(self, msg: dict, uid: int, pid: int) -> dict:
        lid = msg.get("lease")
        if not isinstance(lid, str):
            return _refuse("bad_request", "lease must be a string")
        with self._lock:
            self._purge(self.clock())
            for k, g in self._grants.items():
                if g.id == lid and (g.uid == uid or uid == self.own_uid):
                    del self._grants[k]
                    self.audit.write("release", "ok", name=g.name, host=g.host, uid=uid, pid=pid, lease=g.id)
                    return {"ok": True}
        self.audit.write("release", "expired", uid=uid, pid=pid, lease=lid)
        return _refuse("expired", "no such lease, or it has already ended")

    def _op_status(self) -> dict:
        try:
            problems, owner = wsecrets.health(self.registry, self.store)
        except OSError:
            problems, owner = ["the broker cannot read its own store"], None
        with self._lock:
            self._purge(self.clock())
            n = len(self._grants)
        return {
            "ok": True,
            "version": __version__,
            "uid": self.own_uid,
            "port": self.port,
            "leases": n,
            "store_owner_uid": owner,
            "problems": problems,
            "code_problem": code_problem(),
            "elevated": self.elevation_on(),
            "require_run": self.require_run,
        }

    def _scope(
        self, entry: wsecrets.Entry, msg: dict, uid: int, log: dict, now: float, need_run: bool
    ) -> tuple[RunRecord | None, tuple[str, str] | None]:
        """The run record behind a request and None, or (None, (code, reason)) for the refusal. `log` gains the run's
        area and tier, so the audit line says what the broker read."""
        name, run_id = entry.name, msg.get("run")
        if run_id is None:
            if entry.scoped or self.require_run or need_run:
                return None, ("run_required", f"{name} is leased only under a run: send the run id")
            if msg.get("area") is not None or msg.get("tier") is not None:
                return None, ("claim_refused", "an area or tier is read from the run record, never from a request")
            rec = None
        else:
            try:
                rec = self.run_record(run_id, now)
            except RecordError as e:
                return None, ("run_refused", f"no usable run record ({e.kind}): {e}")
            log.update(area=rec.area, tier=rec.tier)
            for claim in ("area", "tier"):
                if msg.get(claim) is not None and msg.get(claim) != getattr(rec, claim):
                    return None, ("claim_refused", f"the request's {claim} is not the one the run record holds")
            if rec.uid is not None and rec.uid != uid:
                return None, ("caller_refused", "this caller is not the one the run record names")
            if name not in rec.names:
                return None, ("not_declared", f"{name} is not one of the names this run declared")
            if entry.area is not None and entry.area != rec.area:
                return None, ("area_refused", f"{name} belongs to another area")
            if (entry.tier or wsecrets.DEFAULT_TIER) not in wsecrets.REACH[rec.tier]:
                return None, ("tier_refused", f"a {rec.tier} run cannot lease {name}")
        if entry.elevated and not self.elevation_on():
            return None, ("elevation_off", f"{name} is elevated and `ws elevate off` is in force")
        return rec, None

    def _minter_entry(self, msg: dict, op: str, kind: str, log: dict) -> tuple[wsecrets.Entry | None, dict | None]:
        name = msg.get("name")
        if not (isinstance(name, str) and wsecrets.NAME_RE.match(name)):
            return None, self._refuse_logged(op, log, "bad_request", "name must be a secret name")
        try:
            entry = wsecrets.load_registry(self.registry).get(name)
        except wsecrets.SecretsError:
            return None, self._refuse_logged(op, log, "store_unreadable", "the registry cannot be read")
        if entry is None:
            return None, self._refuse_logged(op, log, "unknown_name", f"{name} is not in the registry")
        if entry.kind != kind:
            return None, self._refuse_logged(op, log, "wrong_kind", f"{name} is not a {kind} entry")
        return entry, None

    def _refuse_logged(self, op: str, log: dict, code: str, reason: str) -> dict:
        self.audit.write(op, code, **log)
        return _refuse(code, reason)

    def _stored(self, op: str, log: dict, name: str) -> tuple[str | None, dict | None]:
        try:
            return self.store.read(name), None
        except FileNotFoundError:
            return None, self._refuse_logged(op, log, "unset", f"{name} has no value; run `ws secret set {name}`")
        except wsecrets.SecretsError as e:
            return None, self._refuse_logged(op, log, "store_unreadable", str(e))

    def _op_mint(self, msg: dict, uid: int, pid: int) -> dict:
        """A GitHub App installation token for a run, narrowed to the repos and permissions it asks for."""
        log = {"name": msg.get("name"), "uid": uid, "pid": pid, "run": msg.get("run"), "host": None}
        entry, refused = self._minter_entry(msg, "mint", github_app.KIND, log)
        if refused:
            return refused
        assert entry is not None and isinstance(entry.options, github_app.Options)
        opts = entry.options
        log["host"] = (urlsplit(opts.api).hostname or "").lower() or None
        now = self.clock()
        self.reap_minted()
        rec, why = self._scope(entry, msg, uid, log, now, need_run=True)
        if why:
            return self._refuse_logged("mint", log, *why)
        assert rec is not None
        try:
            repos, perms = github_app.narrow(
                opts, msg.get("repos"), msg.get("permissions"), rec.repos, dict(rec.permissions)
            )
        except github_app.MintError as e:
            return self._refuse_logged("mint", log, e.code, e.reason)
        log["purpose"] = ("repos=" + ",".join(repos) + " " + ",".join(f"{k}:{v}" for k, v in perms.items()))[:120]
        key = (entry.name, rec.run, repos, tuple(perms.items()))
        with self._lock:
            hit = self._mint_cache.get(key)
        if hit is not None and hit[1] - now > MINT_REUSE_MARGIN_S:
            token, expires = hit
        else:
            value, refused = self._stored("mint", log, entry.name)
            if refused:
                return refused
            assert value is not None
            try:
                token, expires = github_app.mint(opts, value, repos, perms, now, self.allow_plain_http)
            except github_app.MintError as e:
                return self._refuse_logged("mint", log, e.code, e.reason)
            finally:
                del value
            with self._lock:
                late = entry.elevated and not self.elevation_on()
                if not late:
                    self._mint_cache[key] = (token, expires)
                    self._minted.append(_Minted(entry.name, rec.run, entry.elevated, token, opts, expires))
            if late:  # `ws elevate off` came while GitHub was answering: the token never leaves the broker
                self._revoke_one(_Minted(entry.name, rec.run, entry.elevated, token, opts, expires))
                return self._refuse_logged(
                    "mint", log, "elevation_off", f"{entry.name} is elevated and was switched off"
                )
        if rec.expires_at is not None and rec.expires_at < expires:
            expires_out = rec.expires_at  # the caller is told the run's end; the token itself cannot be shortened
        else:
            expires_out = expires
        if not self.audit.write("mint", "ok", **log):
            return _refuse("audit_failed", "the token could not be written to the audit log, so it is not given")
        return {"ok": True, "token": token, "expires_at": _iso(expires_out)}

    def _op_sign(self, msg: dict, uid: int, pid: int) -> dict:
        """An SSH certificate for a run's own public key, principals from its tier."""
        log = {"name": msg.get("name"), "uid": uid, "pid": pid, "run": msg.get("run"), "host": None}
        entry, refused = self._minter_entry(msg, "sign", ssh_ca.KIND, log)
        if refused:
            return refused
        assert entry is not None and isinstance(entry.options, ssh_ca.Options)
        rec, why = self._scope(entry, msg, uid, log, self.clock(), need_run=True)
        if why:
            return self._refuse_logged("sign", log, *why)
        assert rec is not None
        try:
            ssh_ca.check_public_key(msg.get("public_key"))
        except ssh_ca.MintError as e:
            return self._refuse_logged("sign", log, e.code, e.reason)
        value, refused = self._stored("sign", log, entry.name)
        if refused:
            return refused
        assert value is not None
        try:
            out = ssh_ca.sign(entry.options, value, msg["public_key"], rec.run, rec.tier, self.work_dir())
        except ssh_ca.MintError as e:
            return self._refuse_logged("sign", log, e.code, e.reason)
        finally:
            del value
        log["purpose"] = f"serial={out['serial']} principals={','.join(out['principals'])}"[:120]
        if not self.audit.write("sign", "ok", **log):
            return _refuse("audit_failed", "the certificate could not be written to the audit log, so it is not given")
        return {"ok": True, **out}

    def _op_ca_public(self, msg: dict) -> dict:
        """The SSH CA's public key, for the host steps. Public, so any allowed caller may ask."""
        entry, refused = self._minter_entry(msg, "ca_public", ssh_ca.KIND, {"name": msg.get("name")})
        if refused:
            return refused
        assert entry is not None
        value, refused = self._stored("ca_public", {"name": entry.name}, entry.name)
        if refused:
            return refused
        assert value is not None
        try:
            return {"ok": True, "public_key": ssh_ca.ca_public_key(value, self.work_dir())}
        except ssh_ca.MintError as e:
            return _refuse(e.code, e.reason)

    def _take_minted(self, which) -> list[_Minted]:
        """Remove the minted tokens `which(m)` picks from the list and the cache, under the lock, and return them."""
        with self._lock:
            mine = [m for m in self._minted if which(m)]
            gone = {m.token for m in mine}
            self._minted = [m for m in self._minted if m.token not in gone]
            self._mint_cache = {k: v for k, v in self._mint_cache.items() if v[0] not in gone}
        return mine

    def _revoke_one(self, m: _Minted) -> bool:
        try:
            done = github_app.revoke(m.options, m.token, self.allow_plain_http)
        except github_app.MintError:
            done = False
        self.audit.write("revoke", "ok" if done else "failed", name=m.name, run=m.run)
        return done

    def _revoke_all(self, mine: list[_Minted]) -> tuple[int, int]:
        ok = failed = 0
        for m in mine:
            if m.expires <= self.clock():
                continue
            if self._revoke_one(m):
                ok += 1
            else:
                failed += 1
        return ok, failed

    def _revoke_minted(self) -> tuple[int, int]:
        """Revoke upstream every token minted for a name that was elevated when minted or is elevated now; (revoked,
        failed). The cache forgets them."""
        now_elevated = self._elevated_names()  # the registry's tier now: a name re-tiered after minting is caught too
        return self._revoke_all(self._take_minted(lambda m: m.elevated or m.name in now_elevated))

    def reap_minted(self) -> tuple[int, int]:
        """Revoke the tokens of runs that have ended (their record removed, expired or no longer trusted, or no longer
        declaring the name). The reaper thread calls it; so does every mint. (revoked, failed)."""
        with self._lock:
            runs = {(m.run, m.name) for m in self._minted}
        ended = set()
        for run, name in runs:
            try:
                if name not in self.run_record(run).names:
                    ended.add((run, name))
            except RecordError:
                ended.add((run, name))
        if not ended:
            return 0, 0
        return self._revoke_all(self._take_minted(lambda m: (m.run, m.name) in ended))

    def _reaper(self) -> None:
        while not self._stopped.wait(REAP_EVERY_S):
            try:
                self.reap_minted()
            except Exception as e:  # noqa: BLE001 (the reaper keeps going; the type only)
                print(f"ws broker: the token reaper failed ({type(e).__name__})", file=sys.stderr)

    def work_dir(self) -> Path:
        """A private folder of the broker's own for a minter's short-lived key files: never the shared temp folder."""
        if self.records is None:
            raise ssh_ca.MintError("store_unreadable", "this broker has no state folder for key files")
        return self.records.parent / "minter"

    def run_record(self, run_id: object, now: float | None = None) -> RunRecord:
        if self.records is None:
            raise RecordError("missing", "this broker has no run record folder")
        return read_run_record(self.records, run_id, self.own_uid, self.clock() if now is None else now)

    def elevation_on(self) -> bool:
        if self._forced_off:
            return False
        return True if self.elevate_file is None else read_elevation(self.elevate_file, self.own_uid)

    def _op_elevate(self, msg: dict, uid: int, pid: int) -> dict:
        state = msg.get("state")
        if state not in ("on", "off"):
            return _refuse("bad_request", "state must be on or off")
        if state == "on" and uid != self.own_uid:
            self.audit.write("elevate", "caller_refused", uid=uid, pid=pid)
            return _refuse("caller_refused", "only the broker's own user turns elevation back on")
        if state == "on":
            if self.elevate_file is None:
                return _refuse("store_unreadable", "this broker has no elevation file")
            if not self.audit.write("elevate", "on", uid=uid, pid=pid):
                return _refuse("audit_failed", "the audit line could not be written, so elevation stays off")
            try:
                write_elevation(self.elevate_file, True)
            except OSError:
                self.audit.write("elevate", "store_unreadable", uid=uid, pid=pid)
                return _refuse("store_unreadable", "the elevation file cannot be written, so elevation stays off")
            self._forced_off = False
            return {"ok": True, "elevated": True, "saved": True, "dropped": 0, "revoked": 0, "revoke_failed": 0}
        # off: in memory at once, so nothing below can fail open; the file only makes it outlast a restart
        self._forced_off = True
        saved = False
        if self.elevate_file is not None:
            try:
                write_elevation(self.elevate_file, False)
                saved = True
            except OSError:
                self.audit.write("elevate", "store_unreadable", uid=uid, pid=pid)
        dropped = revoked = failed = 0
        elevated_now = self._elevated_names()
        with self._lock:
            gone = [(k, g) for k, g in self._grants.items() if g.elevated or g.name in elevated_now]
            for k, _ in gone:
                del self._grants[k]
        for _, g in gone:
            dropped += 1
            for revoke in self.revokers:
                try:
                    done = revoke(g)
                except Exception:  # noqa: BLE001 (a revoker's failure is counted and logged, never fatal)
                    failed += 1
                    self.audit.write("revoke", "failed", name=g.name, host=g.host, lease=g.id, run=g.run)
                    continue
                if done:
                    revoked += 1
                    self.audit.write("revoke", "ok", name=g.name, host=g.host, lease=g.id, run=g.run)
        r_ok, r_failed = self._revoke_minted()
        revoked, failed = revoked + r_ok, failed + r_failed
        self.audit.write("elevate", "off" if saved else "off_unsaved", uid=uid, pid=pid, bytes=dropped)
        return {
            "ok": True,
            "elevated": False,
            "saved": saved,
            "dropped": dropped,
            "revoked": revoked,
            "revoke_failed": failed,
        }

    def _elevated_names(self) -> set[str]:
        """The names the registry marks elevated now. A registry that cannot be read: every live grant's name, so a
        kill switch pulled while the registry is broken still drops everything."""
        try:
            return {e.name for e in wsecrets.load_registry(self.registry).values() if e.elevated}
        except wsecrets.SecretsError:
            with self._lock:
                return {g.name for g in self._grants.values()} | {m.name for m in self._minted}

    def _purge(self, now: float) -> None:
        for k in [k for k, g in self._grants.items() if g.expires <= now]:
            del self._grants[k]
        self._minted = [m for m in self._minted if m.expires > now]
        self._mint_cache = {k: v for k, v in self._mint_cache.items() if v[1] > now}

    # -- the proxy side

    def grant_for(self, token: str) -> _Grant:
        """The live grant for a lease token. Raises _HttpError."""
        k = _key(token)
        with self._lock:
            g = self._grants.get(k)
            if g is None:
                raise _HttpError(403, "lease_invalid")
            if g.expires <= self.clock():
                del self._grants[k]
                raise _HttpError(403, "expired")
            return g

    def credential(self, g: _Grant) -> tuple[str, str]:
        """(header name, header value) for a grant, read fresh so a removed or changed secret takes effect at once."""
        try:
            entry = wsecrets.load_registry(self.registry).get(g.name)
        except wsecrets.SecretsError as e:
            raise _HttpError(503, "store_unreadable") from e
        if entry is None or entry.kind != "header" or g.host not in entry.hosts:
            raise _HttpError(403, "host_not_allowed")
        if (g.elevated or entry.elevated) and not self.elevation_on():
            raise _HttpError(403, "elevation_off")
        if g.run is None and entry.scoped:
            raise _HttpError(403, "run_required")  # the name was scoped after the lease was given
        if g.run is not None:
            try:
                rec = self.run_record(g.run)
            except RecordError as e:
                raise _HttpError(403, "run_ended") from e
            if g.name not in rec.names:
                raise _HttpError(403, "not_declared")
            if (rec.area, rec.tier) != (g.area, g.tier) or (rec.uid is not None and rec.uid != g.uid):
                raise _HttpError(403, "run_ended")  # the record was rewritten: the leases under the old one end
            if entry.area is not None and entry.area != rec.area:
                raise _HttpError(403, "area_refused")
            if (entry.tier or wsecrets.DEFAULT_TIER) not in wsecrets.REACH[rec.tier]:
                raise _HttpError(403, "tier_refused")
        try:
            value = self.store.read(g.name)
        except FileNotFoundError as e:
            raise _HttpError(503, "unset") from e
        except wsecrets.SecretsError as e:
            raise _HttpError(503, "store_unreadable") from e
        return entry.header, entry.format.replace("{value}", value)


def code_problem() -> str:
    """Why the code this broker runs could be edited by a non-root user ("" if it cannot). Walled brokers must be clean."""
    pkg = Path(__file__).resolve().parent
    return wsecrets.untrusted_reason(sys.executable) or wsecrets.untrusted_reason(pkg, tree=True) or ""


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _refuse(code: str, reason: str) -> dict:
    return {"ok": False, "code": code, "reason": reason}


def _read_line(conn: socket.socket, buf: bytes, deadline: float) -> tuple[bytes | None, bytes]:
    """The next newline-ended line, or None at end of input, a timeout or an oversize line."""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or len(buf) > MAX_LINE:
            return None, b""
        conn.settimeout(left)
        try:
            chunk = conn.recv(4096)
        except OSError:
            return None, b""
        if not chunk:
            return None, b""
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    return line, rest


_REJECTING = threading.BoundedSemaphore(MAX_REJECTING)


def _reject_async(server: socketserver.BaseServer, request: socket.socket, payload: bytes) -> None:
    """Answer a connection we have no room for, without holding up the accept loop: the reply is written from a short
    thread (it reads what the client sent first, because closing on unread data can reset the reply away). If even
    those threads are all busy the connection is just closed."""
    if not _REJECTING.acquire(blocking=False):
        server.shutdown_request(request)
        return

    def reply() -> None:
        try:
            with contextlib.suppress(OSError):
                request.settimeout(1)
                request.recv(MAX_LINE)
                request.sendall(payload)
        finally:
            server.shutdown_request(request)
            _REJECTING.release()

    threading.Thread(target=reply, daemon=True, name="ws-broker-reject").start()


def _unix_server(broker: Broker) -> socketserver.BaseServer:
    """Built on demand: socketserver has no Unix server on native Windows, and the module must still import there."""

    class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        request_queue_size = MAX_CONNECTIONS

        def __init__(self, broker: Broker):
            self.broker = broker
            super().__init__(str(broker.socket_path), _ConnHandler)

        def process_request(self, request, client_address):
            if not self.broker._slots.acquire(blocking=False):
                _reject_async(self, request, json.dumps(_refuse("busy", "too many connections")).encode() + b"\n")
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.broker._slots.release()

        def handle_error(self, request, client_address):
            print("ws broker: a connection failed", file=sys.stderr)  # not the traceback: it could quote a value

    return _UnixServer(broker)


class _ConnHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.broker.handle_conn(self.request)


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, broker: Broker):
        self.broker = broker
        self._slots = threading.BoundedSemaphore(MAX_PROXY_CONNECTIONS)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            _reject_async(
                self, request, b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_bind(self):
        # HTTPServer.server_bind asks DNS for the host's full name, which can stall for many seconds (seen on macOS)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):
        print("ws broker: a proxy connection failed", file=sys.stderr)


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60
    server: _ProxyServer

    def log_message(self, format, *args):  # the default writes the path, which holds the lease token
        pass

    def handle_one_request(self):
        # The per-read timeout above is reset by every byte, so a client sending one byte a minute would hold a slot
        # for ever. This cuts the connection if the whole request has not arrived by the deadline.
        self._deadline = threading.Timer(REQUEST_DEADLINE_S, self._cut)
        self._deadline.daemon = True
        self._deadline.start()
        try:
            super().handle_one_request()
        finally:
            self._deadline.cancel()

    def _cut(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def do_GET(self):
        self._forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def _forward(self) -> None:
        b = self.server.broker
        grant: _Grant | None = None
        sent = 0
        status_code: int | str = 0
        try:
            path, _, query = self.path.partition("?")
            m = TOKEN_RE.match(path)
            if not m:
                raise _HttpError(404, "not_found")
            grant = b.grant_for(m.group(1))
            header, value = b.credential(grant)
            target = (m.group(2) or "/") + (f"?{query}" if query else "")
            body = self._read_body()
            self._deadline.cancel()  # the request is in; the reply may take as long as the model does
            headers = self._clean_headers(header)
            headers[header] = value
            headers["Host"] = grant.host
            headers["Connection"] = "close"
            status_code, sent = self._relay(grant, target, body, headers)
        except _HttpError as e:
            status_code = e.code
            self._fail(e.status, e.code)
        except (BrokenPipeError, ConnectionResetError):
            status_code = "client_gone"
            self.close_connection = True
        finally:
            b.audit.write(
                "proxy",
                str(status_code),
                name=grant.name if grant else None,
                host=grant.host if grant else None,
                purpose=grant.purpose if grant else None,
                lease=grant.id if grant else None,
                run=grant.run if grant else None,
                area=grant.area if grant else None,
                tier=grant.tier if grant else None,
                bytes=sent,
            )

    def _clean_headers(self, injected: str) -> dict[str, str]:
        out: dict[str, str] = {}
        drop = STRIP_REQUEST | HOP_BY_HOP | {injected}
        for k, v in self.headers.items():
            if k.lower() not in drop:
                out[k] = v
        return out

    def _read_body(self) -> bytes | None:
        """The request body, or None if it has none. A bad size or a short body is a 400, never a truncated forward."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:
                line = self.rfile.readline(1024)
                if not line.endswith(b"\n"):
                    raise _HttpError(400, "bad_request")
                size_text = line.split(b";")[0].strip()
                if not CHUNK_SIZE_RE.match(size_text):
                    raise _HttpError(400, "bad_request")
                size = int(size_text, 16)
                if size == 0:
                    while (trailer := self.rfile.readline(1024)) not in (b"\r\n", b"\n"):
                        if not trailer.endswith(b"\n"):
                            raise _HttpError(400, "bad_request")
                    return bytes(out)
                if len(out) + size > MAX_BODY:
                    raise _HttpError(413, "too_large")
                data = self.rfile.read(size)
                if len(data) != size or self.rfile.read(2) != b"\r\n":
                    raise _HttpError(400, "bad_request")
                out += data
        cl = self.headers.get("Content-Length")
        if cl is None:
            return None
        try:
            n = int(cl)
        except ValueError as e:
            raise _HttpError(400, "bad_request") from e
        if n < 0:
            raise _HttpError(400, "bad_request")
        if n > MAX_BODY:
            raise _HttpError(413, "too_large")
        data = self.rfile.read(n)
        if len(data) != n:
            raise _HttpError(400, "bad_request")
        return data

    def _relay(self, grant: _Grant, target: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, int]:
        hostname, _, port = grant.host.partition(":")
        plain = self.server.broker.allow_plain_http and hostname == "127.0.0.1"
        if plain:
            conn: http.client.HTTPConnection = http.client.HTTPConnection(
                hostname, int(port or 80), timeout=UPSTREAM_TIMEOUT_S
            )
        else:
            conn = http.client.HTTPSConnection(
                hostname, int(port or 443), timeout=UPSTREAM_TIMEOUT_S, context=ssl.create_default_context()
            )
        try:
            try:
                conn.request(self.command, target, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise _HttpError(502, "upstream_unreachable") from e
            bodyless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
            self.send_response_only(resp.status, resp.reason)
            for k, v in resp.msg.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if bodyless:
                if resp.getheader("Content-Length") is not None:
                    self.send_header("Content-Length", resp.getheader("Content-Length"))
            elif resp.length is not None:
                self.send_header("Content-Length", str(resp.length))
            else:
                self.send_header("Connection", "close")  # length unknown: the end of the body is the end of the stream
                self.close_connection = True
            self.end_headers()
            sent = 0
            while not bodyless:
                try:
                    chunk = resp.read1(65536)
                except (OSError, http.client.HTTPException):
                    self.close_connection = True
                    break
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
            return resp.status, sent
        finally:
            conn.close()

    def _fail(self, status: int, code: str) -> None:
        data = json.dumps({"error": code}).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


# ---------------------------------------------------------------- the command


def _hint(e: Exception) -> str:
    return e.hint if isinstance(e, SocketPathTooLong) else "is the broker running? `ws doctor` checks"


def _require_run_default() -> bool:
    try:
        return wsecrets._profile_secrets(None).get("require_run") is True
    except wsecrets.SecretsError:
        return True  # a profile that cannot be read: the stricter rule


def cmd_serve(args: argparse.Namespace) -> int:
    import signal

    try:
        broker = Broker.from_env(
            args.socket, args.group, args.port, args.lease_ttl, require_run=args.require_run or _require_run_default()
        )
        signal.signal(signal.SIGTERM, lambda *_: broker.request_stop())
        signal.signal(signal.SIGINT, lambda *_: broker.request_stop())
        broker.start()
    except (BrokerError, wsecrets.SecretsError, OSError) as e:
        raise WsError("unavailable", str(e), e.hint if isinstance(e, SocketPathTooLong) else "") from e
    print(f"ws broker: socket {broker.socket_path}, leases on 127.0.0.1:{broker.port}", file=sys.stderr, flush=True)
    broker._stopped.wait()
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    try:
        lease = request(args.name, args.purpose, args.host, socket=args.socket, ttl_s=args.ttl, run=args.run)
    except BrokerRefused as e:
        raise WsError("refused", str(e)) from e
    except BrokerUnavailable as e:
        raise WsError("unavailable", str(e), _hint(e)) from e
    if args.json:
        output.result(lease.__dict__)
    else:
        print(json.dumps(lease.__dict__))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        info = status(args.socket)
    except BrokerError as e:
        raise WsError("unavailable", str(e), _hint(e)) from e
    if args.json:
        output.result(info)
    else:
        print(json.dumps(info, indent=2))
    return 0


def cmd_run_record(args: argparse.Namespace) -> int:
    folder = Path(args.dir).expanduser() if args.dir else records_dir()
    try:
        if args.record_command == "write":
            expires = None
            if args.ttl is not None:
                if not args.ttl > 0:
                    raise WsError("bad_input", "--ttl must be above 0", "seconds the run may lease for", 2)
                expires = time.time() + args.ttl
            perms = {}
            for item in args.perm or ():
                k, _, v = item.partition("=")
                perms[k] = v
            rec = RunRecord(
                run=args.run,
                area=args.area,
                tier=args.tier,
                names=tuple(dict.fromkeys(args.name or ())),
                uid=args.uid,
                expires_at=expires,
                repos=tuple(dict.fromkeys(args.repo or ())),
                permissions=tuple(sorted(perms.items())),
            )
            where = write_run_record(folder, rec)
            out: dict = {"run": rec.run, "written": str(where), **rec.to_json()}
            line = f"ws: run record {rec.run} ({rec.tier}, area {rec.area}, {len(rec.names)} names) at {where}"
        elif args.record_command == "rm":
            gone = remove_run_record(folder, args.run)
            out = {"run": args.run, "removed": gone}
            line = f"ws: removed run record {args.run}" if gone else f"ws: no run record {args.run} (0 changes)"
        else:
            rec = read_run_record(folder, args.run, os.geteuid())
            out = rec.to_json()
            line = json.dumps(out, indent=2)
    except RecordError as e:
        raise WsError(
            "refused" if e.kind == "untrusted" else "failed", str(e), "see `ws help broker run-record`"
        ) from e
    except OSError as e:
        raise WsError("failed", f"the run record folder {folder}: {e.strerror or type(e).__name__}") from e
    if args.json:
        output.result(out)
    else:
        print(line)
    return 0


SCP_TARGET = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[A-Za-z0-9_./~+-]*$")
LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def audit_copy(src: Path, dest: str, label: str, run=None) -> int:
    """Copy the audit log to `dest` as `broker-<label>.jsonl`: a folder (an off-box mount), or an scp target
    `[user@]host:folder` (key auth only, no prompt). Returns the bytes copied. Raises OSError or WsError."""
    import shutil
    import subprocess

    data_len = src.stat().st_size
    name = f"broker-{LABEL_RE.sub('-', label).strip('-') or 'ws'}.jsonl"
    if SCP_TARGET.match(dest) and not dest.startswith(("/", "~", ".")):
        target = dest if dest.endswith(":") else dest.rstrip("/") + "/"
        r = (run or subprocess.run)(
            ["scp", "-q", "-B", "-o", "BatchMode=yes", "--", str(src), target + name],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if r.returncode != 0:
            raise WsError(
                "failed", f"scp exited {r.returncode}", "check the key and the target folder; no prompt is used"
            )
        return data_len
    folder = Path(dest).expanduser()
    if not folder.is_dir():
        raise WsError("not_found", f"{folder} is not a folder", "mount the off-box share there, or set an scp target")
    tmp = folder / f".{name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        os.replace(tmp, folder / name)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return data_len


def cmd_audit_copy(args: argparse.Namespace) -> int:
    try:
        cfg = wsecrets._profile_secrets(None)
    except wsecrets.SecretsError as e:
        raise WsError("bad_config", str(e)) from e
    dest = args.to or cfg.get("audit_copy")
    if not isinstance(dest, str) or not dest:
        raise WsError(
            "not_configured",
            "no destination for the audit copy",
            'set `audit_copy = "<folder or user@host:folder>"` under [roles.secrets] in the profile, or pass --to',
            1,
        )
    src = paths.state_dir() / "logs" / "broker.jsonl"
    if not src.is_file():
        raise WsError("not_found", f"no audit log at {src} yet", "the broker writes it on its first request", 1)
    label = cfg.get("audit_label") if isinstance(cfg.get("audit_label"), str) else socket.gethostname()
    try:
        n = audit_copy(src, dest, label)
    except OSError as e:
        raise WsError("failed", f"the audit copy failed: {e.strerror or type(e).__name__}") from e
    if args.json:
        output.result({"copied_bytes": n, "to": dest})
    else:
        print(f"ws: copied {n} bytes of the broker audit log to {dest}")
    return 0


def cmd_elevate(args: argparse.Namespace) -> int:
    from ws import agentsession

    if args.state == "status":
        try:
            info = status(args.socket)
        except BrokerError:
            on = read_elevation(elevate_path(), os.geteuid())
            out = {"elevated": on, "broker": False}
        else:
            out = {"elevated": bool(info.get("elevated")), "broker": True}
        if args.json:
            output.result(out)
        else:
            print(f"ws: elevation is {'on' if out['elevated'] else 'off'}" + ("" if out["broker"] else " (no broker)"))
        return 0
    on = args.state == "on"
    if on:  # pulling the switch is anyone's; putting it back is a person's, never an agent session's
        why = agentsession.refusal(os.environ, sys.stdin.isatty(), sys.stdout.isatty())
        if why:
            raise WsError("refused", f"ws elevate on: {why}", "run it in your own terminal", 3)
    try:
        out = elevate(on, args.socket)
        out["broker"] = True
    except BrokerRefused as e:
        raise WsError("refused", str(e)) from e
    except BrokerUnavailable as e:
        if not e.not_running:
            raise WsError("unavailable", str(e), _hint(e)) from e
        why = _not_this_users_broker(args.socket)
        if why:
            raise WsError(
                "unavailable",
                f"no broker is answering, and its switch is not this user's to write: {why}",
                "start the broker, or run this as the broker's user: "
                f"`sudo -u {wsecrets.WALL_USER} env WS_HOME={wsecrets.WALL_HOME} {wsecrets.WALL_BIN} elevate off`",
            ) from e
        try:
            write_elevation(elevate_path(), on)
        except OSError as e2:
            raise WsError("failed", f"no broker running, and {elevate_path()} cannot be written") from e2
        out = {"elevated": on, "broker": False, "saved": True, "dropped": 0, "revoked": 0, "revoke_failed": 0}
    if args.json:
        output.result(out)
        return 0
    if not out.get("saved", True):
        print(
            "ws: elevation is off in the running broker, but the switch file could not be saved: a restart would"
            " turn it back on. Fix the broker's state folder, then run `ws elevate off` again.",
            file=sys.stderr,
        )
    if on:
        print("ws: elevation is on: elevated names can be leased again by runs whose tier reaches them")
    elif out["broker"]:
        print(
            f"ws: elevation is off: elevated names are refused; {out['dropped']} leases dropped, {out['revoked']} "
            f"upstream grants revoked, {out['revoke_failed']} could not be revoked"
        )
    else:
        print(f"ws: elevation is off (no broker running; {elevate_path()} says so for when one starts)")
    return 1 if out.get("revoke_failed") or not out.get("saved", True) else 0


def _not_this_users_broker(socket_flag: str | None) -> str:
    """Why the switch file this user would write is not the one the broker reads ("" when it is): the broker is behind
    the wall, or is named by a socket the profile, the environment or the flag points elsewhere."""
    try:
        cfg = wsecrets._profile_secrets(None)
    except wsecrets.SecretsError:
        return "the profile cannot be read"
    store = wsecrets.store_dir(None, cfg)
    try:
        if store.stat().st_uid != os.geteuid():
            return f"the store {store} belongs to another user (the broker runs behind the wall)"
    except FileNotFoundError:
        pass
    except OSError:
        return f"the store {store} cannot be read from here"
    if socket_flag or os.environ.get("WS_BROKER_SOCKET") or cfg.get("socket"):
        return "the broker's socket is set explicitly, so its state folder may not be this user's"
    return ""


def register_elevate(sub) -> None:
    e = sub.add_parser("elevate", help="the kill switch for elevated secrets: `ws elevate off` refuses them at once")
    e.add_argument("state", choices=("off", "on", "status"), help="off: refuse and revoke; on: allow again; status")
    e.add_argument("--socket", metavar="PATH")
    e.set_defaults(func=cmd_elevate)


def register(sub) -> None:
    b = sub.add_parser("broker", help="the secrets broker: agents ask it to use a key and never see the value")
    bsub = b.add_subparsers(dest="broker_command", required=True)
    p = bsub.add_parser("serve", help="run the broker (a Unix socket and a loopback lease proxy)")
    p.add_argument(
        "--socket",
        metavar="PATH",
        help="default $WS_BROKER_SOCKET, else [roles.secrets] socket in the profile, else <state>/run/broker.sock",
    )
    p.add_argument("--group", metavar="GROUP", help="also serve members of this group (socket folder becomes 0750)")
    p.add_argument("--port", type=int, default=0, help="lease proxy port on 127.0.0.1 (default: any free one)")
    p.add_argument(
        "--lease-ttl", type=float, default=DEFAULT_TTL_S, metavar="SECONDS", help="longest lease (default 3600)"
    )
    p.add_argument(
        "--require-run",
        action="store_true",
        help="every lease needs a run record, scoped name or not (also [roles.secrets] require_run = true)",
    )
    p.set_defaults(func=cmd_serve, raw=True)  # it stays up until stopped
    p = bsub.add_parser("request", help="ask for a lease and print it as JSON (for scripts)")
    p.add_argument("name")
    p.add_argument("--purpose", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--ttl", type=float, metavar="SECONDS", help="ask for a shorter lease")
    p.add_argument("--run", metavar="RUN_ID", help="the run the lease is for (its run record says what it may have)")
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_request)
    p = bsub.add_parser("status", help="ask a running broker how it is")
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_status)
    p = bsub.add_parser("run-record", help="write, show or remove a run record (what one run may lease)")
    rsub = p.add_subparsers(dest="record_command", required=True)
    w = rsub.add_parser("write", help="write the record for a run (the launcher does this for a job)")
    w.add_argument("run", help="the run id")
    w.add_argument("--area", required=True, help="the run's area")
    w.add_argument("--tier", required=True, choices=wsecrets.TIERS, help="the run's tier")
    w.add_argument("--name", action="append", metavar="NAME", help="a secret name the run declares (repeat)")
    w.add_argument("--uid", type=int, help="only this caller uid may lease under the run")
    w.add_argument("--ttl", type=float, metavar="SECONDS", help="the run ends after this; its leases with it")
    w.add_argument(
        "--repo", action="append", metavar="REPO", help="a repository a minted GitHub token may cover (repeat)"
    )
    w.add_argument("--perm", action="append", metavar="NAME=LEVEL", help="the most a minted token may hold (repeat)")
    for verb, text in (("show", "print a run record"), ("rm", "end a run: remove its record and its leases")):
        r = rsub.add_parser(verb, help=text)
        r.add_argument("run")
        r.add_argument("--dir", metavar="PATH", help="the record folder (default <state>/run/records)")
    w.add_argument("--dir", metavar="PATH", help="the record folder (default <state>/run/records)")
    p.set_defaults(func=cmd_run_record)
    p = bsub.add_parser("audit-copy", help="copy the audit log off this machine (a job runs it daily)")
    p.add_argument("--to", metavar="DEST", help="a folder or user@host:folder (default [roles.secrets] audit_copy)")
    p.set_defaults(func=cmd_audit_copy)
