"""The broker: socket protocol, every refusal code, expiry, header stripping and injection against a local stub
upstream, streaming, and that no value or lease token ever reaches a log, a record or an error. No network: the
upstream is a stub on 127.0.0.1 (WS_BROKER_TEST_HTTP_UPSTREAM allows plain http to 127.0.0.1 only)."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ws import broker
from ws import secrets as wsecrets

VALUE = "planted-" + os.urandom(12).hex()


class Stub(ThreadingHTTPServer):
    """Records every request it gets: method, target, headers (lowercased names), body."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.seen: list[dict] = []
        self.go = threading.Event()  # set by a test to let /sse send its second event

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _record(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if "chunked" in (self.headers.get("Transfer-Encoding") or ""):
            body = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if not size:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                self.rfile.readline()
        self.server.seen.append(
            {
                "method": self.command,
                "target": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        return body

    def do_GET(self):
        body = self._record()
        if self.path.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b"data: one\n\n")
            if not self.server.go.wait(10):
                return
            self._chunk(b"data: two\n\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path.startswith("/noreply"):
            self.send_response(204)
            self.end_headers()
            return
        out = json.dumps({"ok": True, "len": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("X-Upstream", "yes")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    do_POST = do_PUT = do_HEAD = do_GET

    def _chunk(self, data: bytes):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv(broker.TEST_HTTP_ENV, "1")
    s = Stub()
    threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True).start()
    yield s
    s.go.set()
    s.shutdown()
    s.server_close()


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="wsb"))  # short: Unix socket paths top out near 100 characters
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Env:
    def __init__(self, base: Path, stub: Stub):
        self.base, self.stub = base, stub
        self.registry = base / "secrets.toml"
        self.store = wsecrets.Store(base / "secrets")
        self.log = base / "logs" / "broker.jsonl"
        self.sock = base / "run" / "b.sock"
        self.now = time.time()
        self.broker: broker.Broker | None = None

    def write_registry(self, text: str | None = None):
        self.registry.write_text(
            text
            or f'[anthropic]\nhosts = ["{self.stub.host}"]\npurposes = ["run", "review"]\n\n'
            f'[github]\nhosts = ["{self.stub.host}"]\nheader = "authorization"\n',
            encoding="utf-8",
        )

    def start(self, **kw) -> broker.Broker:
        self.broker = broker.Broker(
            self.sock, self.registry, self.store, log_path=self.log, clock=lambda: self.now, **kw
        ).start()
        return self.broker

    def lease(self, name="anthropic", purpose="run", host=None, **kw) -> broker.Lease:
        return broker.request(name, purpose, host or self.stub.host, socket=self.sock, ttl_s=kw.get("ttl_s"))

    def call(self, path="/v1/x", method="GET", headers=None, body=None, lease=None):
        lease = lease or self.lease()
        u = lease.base_url.removeprefix("http://")
        hostport, _, prefix = u.partition("/")
        c = http.client.HTTPConnection(hostport, timeout=10)
        c.request(method, "/" + prefix + path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def audit(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def env(base, stub):
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.store.write("github", VALUE)
    e.start()
    yield e
    e.broker.stop()


def refusal(fn, *a, **k) -> broker.BrokerRefused:
    with pytest.raises(broker.BrokerRefused) as e:
        fn(*a, **k)
    return e.value


def test_request_returns_a_lease_on_loopback(env):
    lease = env.lease(ttl_s=60)
    assert lease.base_url.startswith(f"http://127.0.0.1:{env.broker.port}/l/")
    assert lease.header == "x-api-key" and len(lease.lease) == 12
    assert lease.expires_at.endswith("Z")
    assert env.lease(name="github").header == "authorization"
    assert VALUE not in repr(lease)


def test_every_refusal_code(env, monkeypatch):
    s = env.sock
    assert refusal(broker.request, "nothere", "run", env.stub.host, socket=s).code == "unknown_name"
    assert refusal(broker.request, "anthropic", "run", "evil.example.com", socket=s).code == "host_not_allowed"
    assert refusal(broker.request, "anthropic", "wander", env.stub.host, socket=s).code == "purpose_not_allowed"
    env.store.delete("github")
    assert refusal(broker.request, "github", "anything", env.stub.host, socket=s).code == "unset"
    (env.store.path / "anthropic").chmod(0o644)
    assert refusal(broker.request, "anthropic", "run", env.stub.host, socket=s).code == "store_unreadable"
    (env.store.path / "anthropic").chmod(0o600)
    env.registry.write_text("[broken", encoding="utf-8")
    assert refusal(broker.request, "anthropic", "run", env.stub.host, socket=s).code == "store_unreadable"
    env.write_registry()
    monkeypatch.setattr(broker, "peer_cred", lambda conn: (1, os.geteuid() + 12345))
    assert refusal(broker.request, "anthropic", "run", env.stub.host, socket=s).code == "caller_refused"
    assert refusal(broker.status, s).code == "caller_refused"
    monkeypatch.undo()
    assert refusal(broker.release, "0123456789ab", socket=s).code == "expired"


def test_bad_requests_get_bad_request(env):
    def raw(payload: bytes) -> dict:
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(env.sock))
        c.sendall(payload)
        out = c.makefile().readline()
        c.close()
        return json.loads(out)

    for payload in (
        b"not json\n",
        b"[1]\n",
        b'{"op":"nope"}\n',
        b'{"op":"request","name":5,"purpose":"","host":"x"}\n',
    ):
        r = raw(payload)
        assert r["ok"] is False and r["code"] == "bad_request"
    r = raw(
        b'{"op":"request","name":"anthropic","purpose":"run","host":"' + env.stub.host.encode() + b'","ttl_s":-1}\n'
    )
    assert r["code"] == "bad_request"
    r = raw(b'{"op":"request","name":"../etc","purpose":"run","host":"x"}\n')
    assert r["code"] == "bad_request"


def test_more_than_one_request_per_connection(env):
    c = socket.socket(socket.AF_UNIX)
    c.connect(str(env.sock))
    f = c.makefile("rw")
    for _ in range(2):
        f.write('{"op":"status"}\n')
        f.flush()
        assert json.loads(f.readline())["ok"] is True
    c.close()


def test_status_reports_health_without_values(env):
    st = broker.status(env.sock)
    assert st["problems"] == [] and st["leases"] == 0 and st["port"] == env.broker.port
    assert st["store_owner_uid"] == os.geteuid()
    env.lease()
    assert broker.status(env.sock)["leases"] == 1
    (env.store.path / "anthropic").chmod(0o644)
    assert any("0644" in p for p in broker.status(env.sock)["problems"])
    assert VALUE not in json.dumps(st)


def test_proxy_strips_incoming_auth_and_injects_the_real_header(env):
    status, hdrs, data = env.call(
        "/v1/messages?beta=1",
        method="POST",
        body=b'{"hi":1}',
        headers={
            "x-api-key": "placeholder",
            "Authorization": "Bearer wrong",
            "Cookie": "a=b",
            "Proxy-Authorization": "x",
            "X-Custom": "kept",
            "Content-Type": "application/json",
        },
    )
    assert status == 200 and hdrs["X-Upstream"] == "yes" and json.loads(data) == {"ok": True, "len": 8}
    seen = env.stub.seen[-1]
    h = seen["headers"]
    assert seen["method"] == "POST" and seen["target"] == "/v1/messages?beta=1" and seen["body"] == b'{"hi":1}'
    assert h["x-api-key"] == VALUE
    assert not {"authorization", "cookie", "proxy-authorization"} & h.keys()
    assert h["x-custom"] == "kept" and h["content-type"] == "application/json"
    assert h["host"] == env.stub.host


def test_authorization_secrets_use_the_registry_format(env):
    env.call(headers={"X-Api-Key": "placeholder", "Authorization": "Bearer wrong"}, lease=env.lease(name="github"))
    h = env.stub.seen[-1]["headers"]
    assert h["authorization"] == f"Bearer {VALUE}"
    assert "x-api-key" not in h  # the tool's own x-api-key is credential-bearing and dropped


def test_lease_for_one_host_cannot_reach_another(env):
    # the target is fixed by the lease: a path that looks like a URL or another host stays a path on that host
    env.call("//evil.example.com/x")
    env.call("/http://evil.example.com/")
    # (the stub's own http.server collapses a leading "//", so only the end of the first target is compared)
    assert [s["target"].lstrip("/") for s in env.stub.seen] == ["evil.example.com/x", "http://evil.example.com/"]
    assert all(s["headers"]["host"] == env.stub.host for s in env.stub.seen)


def test_chunked_request_bodies_are_forwarded(env):
    lease = env.lease()
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request(
        "POST",
        f"/{prefix}/v1/x",
        body=iter([b"ab", b"cdef"]),
        headers={"Transfer-Encoding": "chunked"},
        encode_chunked=True,
    )
    r = c.getresponse()
    r.read()
    assert r.status == 200 and env.stub.seen[-1]["body"] == b"abcdef"


def test_head_and_no_content_responses(env):
    status, hdrs, data = env.call(method="HEAD")
    assert status == 200 and data == b"" and int(hdrs["Content-Length"]) > 0
    status, _, data = env.call("/noreply")
    assert status == 204 and data == b""


def test_streaming_passes_through_without_buffering(env):
    lease = env.lease()
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("GET", f"/{prefix}/sse")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    first = r.read1(100)  # arrives while the upstream is still holding the second event back
    assert first == b"data: one\n\n"
    assert not env.stub.go.is_set()
    env.stub.go.set()
    rest = b""
    while True:
        part = r.read1(100)
        if not part:
            break
        rest += part
    assert rest == b"data: two\n\n"
    c.close()


def test_plain_http_upstream_is_refused_without_the_test_switch(env, monkeypatch):
    monkeypatch.delenv(broker.TEST_HTTP_ENV)
    status, _, data = env.call()
    assert status == 502 and json.loads(data) == {"error": "upstream_unreachable"}
    assert env.stub.seen == []


def test_unknown_token_and_odd_paths_are_refused(env):
    hostport = env.lease().base_url.removeprefix("http://").partition("/")[0]

    def get(path):
        c = http.client.HTTPConnection(hostport, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()

    assert get("/")[0] == 404 and get("/l/short/x")[0] == 404 and get("/v1/messages")[0] == 404
    assert get("/l/" + "A" * 43 + "/x") == (403, b'{"error": "lease_invalid"}')
    assert env.stub.seen == []


def test_leases_expire_and_release(env):
    short = env.lease(ttl_s=30)
    long = env.lease()
    assert env.call(lease=short)[0] == 200
    env.now += 31
    status, _, data = env.call(lease=short)
    assert status == 403 and json.loads(data) == {"error": "expired"}
    assert json.loads(env.call(lease=short)[2]) == {"error": "lease_invalid"}  # and then it is gone
    assert env.call(lease=long)[0] == 200
    env.now += 3600
    assert env.call(lease=long)[0] == 403
    again = env.lease()
    broker.release(again, socket=env.sock)
    assert env.call(lease=again)[0] == 403
    assert refusal(broker.release, again, socket=env.sock).code == "expired"


def test_expiry_is_reported_as_expired_on_the_proxy_before_a_purge(env):
    lease = env.lease(ttl_s=30)
    env.now += 31
    token = lease.base_url.rsplit("/", 1)[1]
    with pytest.raises(broker._HttpError) as e:
        env.broker.grant_for(token)
    assert e.value.code == "expired"


def test_a_lease_cannot_ask_for_longer_than_the_cap(base, stub):
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start(max_ttl_s=100)
    try:
        lease = e.lease(ttl_s=99999)
        exp = time.mktime(time.strptime(lease.expires_at, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        assert exp - e.now <= 101
    finally:
        e.broker.stop()


def test_release_belongs_to_whoever_asked(env, monkeypatch):
    lease = env.lease()
    other = os.geteuid() + 4242
    monkeypatch.setattr(broker, "peer_cred", lambda conn: (1, other))
    monkeypatch.setattr(broker, "in_group", lambda uid, group: True)
    env.broker.group = "g"
    assert refusal(broker.release, lease, socket=env.sock).code == "expired"
    monkeypatch.undo()
    broker.release(lease, socket=env.sock)  # the broker's own uid may release any


def test_group_members_are_served_and_others_refused(env, monkeypatch):
    other = os.geteuid() + 777
    monkeypatch.setattr(broker, "peer_cred", lambda conn: (1, other))
    assert refusal(broker.status, env.sock).code == "caller_refused"
    env.broker.group = "friends"
    monkeypatch.setattr(broker, "in_group", lambda uid, group: uid == other and group == "friends")
    assert broker.status(env.sock)["problems"] == []
    monkeypatch.setattr(broker, "in_group", lambda uid, group: False)
    assert refusal(broker.status, env.sock).code == "caller_refused"


def test_in_group_uses_the_primary_group_and_the_member_list():
    import grp

    mine = grp.getgrgid(os.getgid()).gr_name
    assert broker.in_group(os.getuid(), mine)
    assert not broker.in_group(os.getuid(), "no-such-group-for-ws")
    assert not broker.in_group(os.getuid(), None)
    assert not broker.in_group(2**31 - 3, mine)


def test_too_many_leases_is_busy(env, monkeypatch):
    monkeypatch.setattr(broker, "MAX_LEASES", 2)
    env.lease()
    env.lease()
    assert refusal(broker.request, "anthropic", "run", env.stub.host, socket=env.sock).code == "busy"


def test_a_store_that_goes_missing_fails_closed_mid_lease(env):
    lease = env.lease()
    env.store.delete("anthropic")
    status, _, data = env.call(lease=lease)
    assert status == 503 and json.loads(data) == {"error": "unset"}
    shutil.rmtree(env.store.path)
    status, _, data = env.call(lease=lease)
    assert status == 503 and json.loads(data) == {"error": "store_unreadable"}
    assert env.stub.seen == []


def test_a_registry_change_takes_effect_on_open_leases(env):
    lease = env.lease()
    env.registry.write_text('[anthropic]\nhosts = ["elsewhere.example.com"]\n', encoding="utf-8")
    status, _, data = env.call(lease=lease)
    assert status == 403 and json.loads(data) == {"error": "host_not_allowed"}
    assert env.stub.seen == []


def test_a_changed_secret_is_used_at_once(env):
    lease = env.lease()
    env.store.write("anthropic", VALUE + "-rotated")
    env.call(lease=lease)
    assert env.stub.seen[-1]["headers"]["x-api-key"] == VALUE + "-rotated"


def test_nothing_secret_reaches_the_audit_log_or_any_reply(env):
    lease = env.lease(purpose="run")
    token = lease.base_url.rsplit("/", 1)[1]
    env.call(lease=lease)
    for code_args in (("nothere", "run", env.stub.host), ("anthropic", "run", "evil.example.com")):
        refusal(broker.request, *code_args, socket=env.sock)
    broker.release(lease, socket=env.sock)
    text = env.log.read_text()
    assert VALUE not in text and token not in text
    recs = env.audit()
    ops = [(r["op"], r["status"]) for r in recs]
    assert ("request", "ok") in ops and ("proxy", "200") in ops and ("release", "ok") in ops
    assert ("request", "unknown_name") in ops and ("request", "host_not_allowed") in ops
    req = next(r for r in recs if r["op"] == "request" and r["status"] == "ok")
    assert req["name"] == "anthropic" and req["host"] == env.stub.host and req["purpose"] == "run"
    assert req["uid"] == os.geteuid() and req["pid"] == os.getpid() and req["lease"] == lease.lease[:6]
    proxy = next(r for r in recs if r["op"] == "proxy")
    assert proxy["bytes"] > 0 and proxy["name"] == "anthropic"
    assert stat.S_IMODE(env.log.stat().st_mode) == 0o600


def test_audit_log_does_not_trust_free_text_names(env):
    c = socket.socket(socket.AF_UNIX)
    c.connect(str(env.sock))
    c.sendall(
        json.dumps({"op": "request", "name": VALUE + "!", "purpose": "p" * 500, "host": VALUE + "!"}).encode() + b"\n"
    )
    c.makefile().readline()
    c.close()
    text = env.log.read_text()
    assert VALUE not in text
    assert max(len(str(r["purpose"])) for r in env.audit()) <= 120


def test_values_never_appear_in_errors(env):
    (env.store.path / "anthropic").write_bytes(b"\xff" + VALUE.encode())
    err = refusal(broker.request, "anthropic", "run", env.stub.host, socket=env.sock)
    assert err.code == "store_unreadable" and VALUE not in str(err)


def test_client_errors_when_nothing_is_listening(base):
    with pytest.raises(broker.BrokerUnavailable) as e:
        broker.request("anthropic", "run", "x.example.com", socket=base / "nope.sock")
    assert str(base) in str(e.value)
    with pytest.raises(broker.BrokerUnavailable):
        broker.status(base)  # a folder is not a socket


def test_socket_is_private_by_default_and_group_shared_when_asked(base, stub):
    import grp

    e = Env(base, stub)
    e.write_registry()
    e.start()
    assert stat.S_IMODE(e.sock.parent.stat().st_mode) == 0o700 and stat.S_IMODE(e.sock.stat().st_mode) == 0o600
    e.broker.stop()
    assert not e.sock.exists()
    mine = grp.getgrgid(os.getgid()).gr_name
    e2 = Env(base / "g", stub)
    (base / "g").mkdir()
    e2.write_registry()
    e2.start(group=mine)
    try:
        assert stat.S_IMODE(e2.sock.parent.stat().st_mode) == 0o750
        assert stat.S_IMODE(e2.sock.stat().st_mode) == 0o660
        assert e2.sock.stat().st_gid == os.getgid()
    finally:
        e2.broker.stop()


def test_unknown_group_and_open_socket_folders_are_refused(base, stub):
    e = Env(base, stub)
    e.write_registry()
    with pytest.raises(broker.BrokerError):
        broker.Broker(e.sock, e.registry, e.store, group="no-such-group-for-ws").start()
    open_dir = base / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    with pytest.raises(broker.BrokerError):
        broker.Broker(open_dir / "b.sock", e.registry, e.store).start()
    assert stat.S_IMODE(open_dir.stat().st_mode) == 0o777  # refused, not "fixed"
    private = base / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(broker.BrokerError) as err:  # a group was asked for but the folder is closed to it
        import grp

        broker.Broker(private / "b.sock", e.registry, e.store, group=grp.getgrgid(os.getgid()).gr_name).start()
    assert "0750" in str(err.value)


def test_stale_socket_is_replaced_but_a_live_one_or_a_file_is_not(base, stub):
    e = Env(base, stub)
    e.write_registry()
    e.sock.parent.mkdir(mode=0o700)
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(e.sock))
    dead.close()  # leaves the file behind with nothing listening
    e.start()
    try:
        with pytest.raises(broker.BrokerError):
            broker.Broker(e.sock, e.registry, e.store).start()
        assert broker.status(e.sock)["uid"] == os.geteuid()  # the first one is untouched
    finally:
        e.broker.stop()
    e.sock.write_text("not a socket")
    with pytest.raises(broker.BrokerError):
        broker.Broker(e.sock, e.registry, e.store).start()


def test_connection_cap_gives_busy(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "MAX_CONNECTIONS", 1)
    e = Env(base, stub)
    e.write_registry()
    e.start()
    try:
        held = socket.socket(socket.AF_UNIX)
        held.connect(str(e.sock))  # holds the one slot: sends nothing
        for _ in range(50):
            if e.broker._slots._value == 0:
                break
            time.sleep(0.05)
        r = refusal(broker.status, e.sock)
        assert r.code == "busy"
        held.close()
    finally:
        e.broker.stop()


def test_a_silent_connection_is_dropped_at_the_deadline(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "CONN_DEADLINE_S", 0.3)
    e = Env(base, stub)
    e.write_registry()
    e.start()
    try:
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(str(e.sock))
        assert c.recv(10) == b""  # the broker closed it
    finally:
        e.broker.stop()


def test_cli_round_trip_through_real_processes(base, stub):
    """ws secret set -> ws broker serve -> ws broker request -> curl-style call through the proxy."""
    home = base / "home"
    home.mkdir(mode=0o700)
    (home / "secrets.toml").write_text(f'[anthropic]\nhosts = ["{stub.host}"]\n', encoding="utf-8")
    envv = {**os.environ, "WS_HOME": str(home), broker.TEST_HTTP_ENV: "1"}
    envv.pop("WS_BROKER_SOCKET", None)
    ws = [sys.executable, "-m", "ws"]
    r = subprocess.run(
        [*ws, "secret", "set", "anthropic"], input=VALUE + "\n", env=envv, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0 and VALUE not in r.stdout + r.stderr
    srv = subprocess.Popen(
        [*ws, "broker", "serve"], env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        sock = home / "run" / "broker.sock"
        for _ in range(300):  # a slow runner can take seconds to start
            try:
                broker.status(sock, timeout=1.0)
                break
            except broker.BrokerError:
                time.sleep(0.1)
        assert broker.status(sock)["problems"] == []
        r = subprocess.run(
            [*ws, "broker", "request", "anthropic", "--purpose", "run", "--host", stub.host],
            env=envv,
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0 and VALUE not in r.stdout + r.stderr
        lease = broker.Lease(**json.loads(r.stdout))
        hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
        c = http.client.HTTPConnection(hostport, timeout=10)
        c.request("POST", f"/{prefix}/v1/messages", body=b"{}", headers={"x-api-key": "placeholder"})
        assert c.getresponse().status == 200
        assert stub.seen[-1]["headers"]["x-api-key"] == VALUE
        r = subprocess.run(
            [*ws, "broker", "request", "nothere", "--purpose", "run", "--host", stub.host],
            env=envv,
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 1 and "unknown_name" in r.stderr
        log = (home / "logs" / "broker.jsonl").read_text()
        assert VALUE not in log and lease.base_url.rsplit("/", 1)[1] not in log
    finally:
        srv.terminate()
        out, err = srv.communicate(timeout=10)
    assert VALUE not in out + err and not sock.exists()


def test_imports_and_doctor_work_without_posix_modules():
    """Native Windows has no pwd, grp or Unix servers: `ws doctor` must still run there and fail WS001 cleanly."""
    code = (
        "import socketserver, sys\n"
        "for m in ('pwd', 'grp', 'fcntl'):\n"
        "    sys.modules[m] = None\n"
        "del socketserver.UnixStreamServer, socketserver.ThreadingUnixStreamServer\n"
        "from ws import broker, cli, doctor, secrets\n"
        "print(secrets.owner_name.__name__)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr[-300:]
