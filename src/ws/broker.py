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

Wire format on the socket: one JSON object per line, one reply per request. Ops: request, release, status.
Refusal codes: unknown_name, unset, host_not_allowed, purpose_not_allowed, caller_refused, store_unreadable, expired,
plus bad_request (malformed) and busy (too many connections or leases).

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

from ws import __version__
from ws import secrets as wsecrets
from ws.profile import ws_home

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
)
DEFAULT_TTL_S = 3600
MAX_CONNECTIONS = 16
MAX_LEASES = 256
CONN_DEADLINE_S = 30.0
MAX_LINE = 65536
MAX_BODY = 64 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 300
MAX_PROXY_CONNECTIONS = 64

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
    """The broker is not running, or this user cannot reach its socket."""


class BrokerRefused(BrokerError):
    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Lease:
    lease: str  # an id for release and the audit log; not a credential
    base_url: str  # point the tool here; the path after it is passed to the allowed host
    header: str  # the header the tool sends its placeholder key in
    expires_at: str  # ISO 8601, UTC


# ---------------------------------------------------------------- client


def _client_call(msg: dict, socket_file: str | Path | None, timeout: float = 10.0) -> dict:
    path = str(socket_file) if socket_file else str(wsecrets.socket_path())
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
        raise BrokerUnavailable(f"no broker answering at {path} ({type(e).__name__})") from e
    finally:
        s.close()
    if not isinstance(reply, dict):
        raise BrokerUnavailable("the broker's reply was not a JSON object")
    if reply.get("ok") is not True:
        raise BrokerRefused(str(reply.get("code", "bad_request")), str(reply.get("reason", "")))
    return reply


def request(name: str, purpose: str, host: str, socket: str | Path | None = None, ttl_s: float | None = None) -> Lease:
    """Ask for a lease. Raises BrokerRefused(code, reason) or BrokerUnavailable."""
    msg: dict = {"op": "request", "name": name, "purpose": purpose, "host": host}
    if ttl_s is not None:
        msg["ttl_s"] = ttl_s
    r = _client_call(msg, socket)["lease"]
    return Lease(lease=r["lease"], base_url=r["base_url"], header=r["header"], expires_at=r["expires_at"])


def release(lease: Lease | str, socket: str | Path | None = None) -> None:
    """End a lease early. Raises BrokerRefused("expired", ...) if it is already gone."""
    _client_call({"op": "release", "lease": lease.lease if isinstance(lease, Lease) else lease}, socket)


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
    """One JSON line per op: time, op, name, purpose, host, caller uid/pid, lease id prefix, status, bytes.
    Never a value or a lease token. Free text from callers is shortened and name/host must look like names/hosts."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, op: str, status: str, **f) -> None:
        if self.path is None:
            return
        name, host = f.get("name"), f.get("host")
        rec = {
            "t": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "op": op,
            "name": name if isinstance(name, str) and wsecrets.NAME_RE.match(name) else (None if name is None else "?"),
            "purpose": str(f["purpose"])[:120] if f.get("purpose") is not None else None,
            "host": host if isinstance(host, str) and wsecrets.HOST_RE.match(host) else (None if host is None else "?"),
            "uid": f.get("uid"),
            "pid": f.get("pid"),
            "lease": (f.get("lease") or "")[:6] or None,
            "status": status,
            "bytes": f.get("bytes"),
        }
        line = (json.dumps(rec, ensure_ascii=True) + "\n").encode()
        try:
            with self._lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
        except OSError:
            print("ws broker: cannot write the audit log", file=sys.stderr)


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

    @classmethod
    def from_env(
        cls,
        socket_file: str | None,
        group: str | None,
        port: int,
        ttl: float,
        env: dict | None = None,
        allow_plain_http: bool = False,
    ):
        env = os.environ if env is None else env
        sock = Path(socket_file).expanduser() if socket_file else ws_home(env) / "run" / "broker.sock"
        return cls(
            sock,
            wsecrets.registry_path(env),
            wsecrets.Store(wsecrets.store_dir(env)),
            group=group,
            log_path=ws_home(env) / "logs" / "broker.jsonl",
            max_ttl_s=ttl,
            port=port,
            allow_plain_http=allow_plain_http,
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
        except Exception as e:  # noqa: BLE001 (answer, never crash the thread; the type only, in case a value is near)
            print(f"ws broker: {op} failed ({type(e).__name__})", file=sys.stderr)
            return _refuse("store_unreadable", "the broker could not complete that")
        return _refuse("bad_request", "unknown op")

    def _op_request(self, msg: dict, uid: int, pid: int) -> dict:
        name, purpose, host, ttl = msg.get("name"), msg.get("purpose", ""), msg.get("host"), msg.get("ttl_s")
        log = {"name": name, "purpose": purpose, "host": host, "uid": uid, "pid": pid}

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
        if entry.purposes and purpose not in entry.purposes:
            return refuse("purpose_not_allowed", f"{name} is not allowed for that purpose")
        try:
            self.store.read(name)
        except FileNotFoundError:
            return refuse("unset", f"{name} has no value; run `ws secret set {name}`")
        except wsecrets.SecretsError as e:
            return refuse("store_unreadable", str(e))
        now = self.clock()
        token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        grant = _Grant(
            id=os.urandom(6).hex(),
            name=name,
            host=host,
            purpose=purpose,
            header=entry.header,
            expires=now + min(float(ttl), self.max_ttl_s) if ttl is not None else now + self.max_ttl_s,
            uid=uid,
            pid=pid,
        )
        with self._lock:
            self._purge(now)
            if len(self._grants) >= MAX_LEASES:
                return refuse("busy", "too many open leases; release some")
            self._grants[_key(token)] = grant
        self.audit.write("request", "ok", lease=grant.id, **log)
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
        }

    def _purge(self, now: float) -> None:
        for k in [k for k, g in self._grants.items() if g.expires <= now]:
            del self._grants[k]

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
        if entry is None or g.host not in entry.hosts:
            raise _HttpError(403, "host_not_allowed")
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
                with contextlib.suppress(OSError):
                    request.settimeout(1)
                    request.recv(
                        MAX_LINE
                    )  # read what the client sent first: closing on unread data can reset the reply away
                    request.sendall(json.dumps(_refuse("busy", "too many connections")).encode() + b"\n")
                self.shutdown_request(request)
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
            with contextlib.suppress(OSError):
                request.settimeout(1)
                request.recv(MAX_LINE)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
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


def cmd_serve(args: argparse.Namespace) -> int:
    import signal

    try:
        broker = Broker.from_env(args.socket, args.group, args.port, args.lease_ttl)
        signal.signal(signal.SIGTERM, lambda *_: broker.request_stop())
        signal.signal(signal.SIGINT, lambda *_: broker.request_stop())
        broker.start()
    except (BrokerError, wsecrets.SecretsError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(f"ws broker: socket {broker.socket_path}, leases on 127.0.0.1:{broker.port}", file=sys.stderr, flush=True)
    broker._stopped.wait()
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    try:
        lease = request(args.name, args.purpose, args.host, socket=args.socket, ttl_s=args.ttl)
    except BrokerRefused as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    except BrokerUnavailable as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(json.dumps(lease.__dict__))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(status(args.socket), indent=2))
    except BrokerError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    b = sub.add_parser("broker", help="the secrets broker: agents ask it to use a key and never see the value")
    bsub = b.add_subparsers(dest="broker_command", required=True)
    p = bsub.add_parser("serve", help="run the broker (a Unix socket and a loopback lease proxy)")
    p.add_argument("--socket", metavar="PATH", help="default $WS_HOME/run/broker.sock")
    p.add_argument("--group", metavar="GROUP", help="also serve members of this group (socket folder becomes 0750)")
    p.add_argument("--port", type=int, default=0, help="lease proxy port on 127.0.0.1 (default: any free one)")
    p.add_argument(
        "--lease-ttl", type=float, default=DEFAULT_TTL_S, metavar="SECONDS", help="longest lease (default 3600)"
    )
    p.set_defaults(func=cmd_serve)
    p = bsub.add_parser("request", help="ask for a lease and print it as JSON (for scripts)")
    p.add_argument("name")
    p.add_argument("--purpose", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--ttl", type=float, metavar="SECONDS", help="ask for a shorter lease")
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_request)
    p = bsub.add_parser("status", help="ask a running broker how it is")
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_status)
