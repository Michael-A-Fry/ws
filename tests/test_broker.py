"""The broker: socket protocol, every refusal code, expiry, header stripping and injection against a local stub
upstream, streaming, and that no value or lease token ever reaches a log, a record or an error. No network: the
upstream is a stub on 127.0.0.1 (WS_BROKER_TEST_HTTP_UPSTREAM allows plain http to 127.0.0.1 only)."""

from __future__ import annotations

import contextlib
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
from helpers.stub_secrets import StubSecrets

from ws import broker
from ws import secrets as wsecrets

VALUE = "planted-" + os.urandom(12).hex()
# `python -c` boot for a child `ws`: its process tree is read as empty (a test run by an agent sits under a runner)
NO_RUNNER_ABOVE = (
    "import sys; from ws import agentsession, cli; agentsession.read_ancestry = list; sys.exit(cli.main(sys.argv[1:]))"
)


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
        if "/sse" in self.path:
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
        if "/echo-loc" in self.path:  # a server that names the path it was asked for, as a redirect would
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.example.com" + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "/echo-head" in self.path:
            out = b"{}"
            self.send_response(200)
            self.send_header("Content-Location", self.path)
            self.send_header("X-Echo-Encoded", self.path.replace(":", "%3A"))
            self.send_header("X-Upstream", "yes")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "/echo-body" in self.path:
            out = b"x" * 70000 + b'{"you_asked": "' + self.path.encode() + b'"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "/echo-split" in self.path:  # the path in two chunks, sent apart, so the broker reads it in two pieces
            half = len(self.path) // 2
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b'{"p": "' + self.path[:half].encode())
            time.sleep(0.2)
            self._chunk(self.path[half:].encode() + b'"}')
            self.wfile.write(b"0\r\n\r\n")
            return
        if "/echo-part" in self.path:  # "/bot" and 20 characters of what follows: an error message that truncates
            out = self.path[:24].encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "/echo-reason" in self.path:
            out = b"{}"
            self.send_response(404, "No route " + self.path)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "/gzip" in self.path:
            out = b"\x1f\x8b" + b"z" * 20
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "/notmod" in self.path:
            self.send_response(304)
            self.send_header("ETag", '"1"')
            self.end_headers()
            return
        if "/noreply" in self.path:
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
def stub():
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
        kw.setdefault("allow_plain_http", True)  # the stub speaks plain http; the real thing never does
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


def test_plain_http_upstream_is_refused_unless_the_constructor_allows_it(env):
    env.broker.allow_plain_http = False
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


def test_serve_socket_comes_from_the_flag_then_env_then_profile_then_home(base):
    home = base / "home"
    home.mkdir()
    (home / "secrets.toml").write_text("", encoding="utf-8")
    envv = {"WS_HOME": str(home)}

    def where(flag=None, **extra):
        return broker.Broker.from_env(flag, None, 0, 60.0, env={**envv, **extra}).socket_path

    assert where() == home / "run" / "broker.sock"
    (home / "profile.toml").write_text(f'[roles.secrets]\nsocket = "{base / "p.sock"}"\n', encoding="utf-8")
    assert where() == base / "p.sock"
    assert where(WS_BROKER_SOCKET=str(base / "e.sock")) == base / "e.sock"
    assert where(str(base / "f.sock"), WS_BROKER_SOCKET=str(base / "e.sock")) == base / "f.sock"


def test_cli_round_trip_through_real_processes(base, stub):
    """ws secret set -> ws broker serve -> ws broker request -> a call through the proxy. The command line cannot be
    told to use plain http, so the call is refused: the stub must see nothing. (Injection is tested above, in process,
    and across users in tests/e2e/test_secret_wall.py.)"""
    home = base / "home"
    home.mkdir(mode=0o700)
    (home / "secrets.toml").write_text(f'[anthropic]\nhosts = ["{stub.host}"]\n', encoding="utf-8")
    envv = {**os.environ, "WS_HOME": str(home)}
    envv.pop("WS_BROKER_SOCKET", None)
    ws = [sys.executable, "-c", NO_RUNNER_ABOVE]  # the process under test: a tree with no runner above it
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
        assert c.getresponse().status == 502
        assert stub.seen == []
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


# ---- request bodies, connection caps, the socket folder, the code check


def raw_proxy(env, payload: bytes, shut_write: bool = True) -> int:
    """Send raw bytes to the proxy under a fresh lease; the HTTP status of the reply."""
    lease = env.lease()
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    host, _, port = hostport.partition(":")
    c = socket.create_connection((host, int(port)), timeout=10)
    c.sendall(f"POST /{prefix}/v1/x HTTP/1.1\r\nHost: x\r\n".encode() + payload)
    if shut_write:
        c.shutdown(socket.SHUT_WR)
    line = c.makefile("rb").readline()
    c.close()
    return int(line.split()[1])


@pytest.mark.parametrize(
    ("payload", "want"),
    [
        (b"Transfer-Encoding: chunked\r\n\r\n-5\r\nabcde\r\n0\r\n\r\n", 400),  # negative size
        (b"Transfer-Encoding: chunked\r\n\r\nzz\r\nab\r\n0\r\n\r\n", 400),  # not hex
        (b"Transfer-Encoding: chunked\r\n\r\n" + b"f" * 9 + b"\r\n", 400),  # too many digits
        (b"Transfer-Encoding: chunked\r\n\r\nffffffff\r\n", 413),  # over the cap, refused before any read
        (b"Transfer-Encoding: chunked\r\n\r\n5\r\nab", 400),  # short chunk
        (b"Transfer-Encoding: chunked\r\n\r\n2\r\nabXX0\r\n\r\n", 400),  # no CRLF after the chunk
        (b"Transfer-Encoding: chunked\r\n\r\n2\r\nab\r\n", 400),  # ends before the last chunk
        (b"Content-Length: 10\r\n\r\nabc", 400),  # short body
        (b"Content-Length: -1\r\n\r\n", 400),
        (b"Content-Length: 99999999999\r\n\r\n", 413),
    ],
)
def test_bad_request_bodies_are_refused_and_never_forwarded(env, payload, want):
    assert raw_proxy(env, payload) == want
    assert env.stub.seen == []


def test_the_proxy_caps_concurrent_connections(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "MAX_PROXY_CONNECTIONS", 1)
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start()
    try:
        lease = e.lease()
        hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
        held = http.client.HTTPConnection(hostport, timeout=10)
        held.request("GET", f"/{prefix}/sse")
        r = held.getresponse()
        assert r.read1(100) == b"data: one\n\n"  # the one slot is now busy until the stream ends
        assert e.call(lease=e.lease())[0] == 503
        stub.go.set()
        r.read()
        held.close()
        for _ in range(50):  # the slot comes back
            if e.call(lease=e.lease())[0] == 200:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the proxy never took another connection")
    finally:
        stub.go.set()
        e.broker.stop()


def test_socket_folder_must_be_the_brokers_and_closed_to_group_write(base, stub):
    import grp

    e = Env(base, stub)
    e.write_registry()
    mine = grp.getgrgid(os.getgid()).gr_name
    shared = base / "shared"
    shared.mkdir()
    shared.chmod(0o770)
    with pytest.raises(broker.BrokerError, match="changed or entered"):
        broker.Broker(shared / "b.sock", e.registry, e.store, group=mine).start()
    private = base / "private"
    private.mkdir(mode=0o700)
    other = broker.Broker(private / "b.sock", e.registry, e.store)
    other.own_uid = os.geteuid() + 1  # the folder is not this broker's
    with pytest.raises(broker.BrokerError, match="belong to the broker"):
        other.start()
    assert not (private / "b.sock").exists()


def test_status_says_where_the_code_runs_from(env):
    # the tests run from a checkout the user can write, which is exactly what a walled broker must report
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert broker.status(env.sock)["code_problem"] != ""


def test_there_is_no_environment_switch_for_plain_http(base, stub, monkeypatch):
    monkeypatch.setenv("WS_BROKER_TEST_HTTP_UPSTREAM", "1")
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start(allow_plain_http=False)
    try:
        assert e.call()[0] == 502
        assert stub.seen == []
    finally:
        e.broker.stop()


def test_a_request_that_trickles_in_is_cut_at_the_deadline(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "REQUEST_DEADLINE_S", 0.5)
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start()
    try:
        lease = e.lease()
        hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
        host, _, port = hostport.partition(":")
        c = socket.create_connection((host, int(port)), timeout=10)
        c.sendall(f"POST /{prefix}/v1/x HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nab".encode())
        t0 = time.monotonic()
        for _ in range(8):  # one byte at a time: every read succeeds, so only the whole-request deadline can stop it
            time.sleep(0.2)
            try:
                c.sendall(b"x")
            except OSError:
                break
        assert time.monotonic() - t0 < 5
        c.settimeout(2)
        with contextlib.suppress(OSError):
            c.recv(100)  # cut: whatever the client sees, nothing was forwarded
        assert stub.seen == []
        c.close()
        # a prompt request still works with the same short deadline
        assert e.call(lease=e.lease())[0] == 200
    finally:
        e.broker.stop()


def test_a_slow_reply_is_not_cut_by_the_request_deadline(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "REQUEST_DEADLINE_S", 0.5)
    e = Env(base, stub)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start()
    try:
        lease = e.lease()
        hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
        c = http.client.HTTPConnection(hostport, timeout=10)
        c.request("GET", f"/{prefix}/sse")
        r = c.getresponse()
        assert r.read1(100) == b"data: one\n\n"
        time.sleep(1.0)  # past the request deadline: the reply is still streaming
        stub.go.set()
        assert r.read() == b"data: two\n\n"
        c.close()
    finally:
        stub.go.set()
        e.broker.stop()


def test_a_connection_with_no_room_does_not_hold_up_the_accept_loop(base, stub, monkeypatch):
    monkeypatch.setattr(broker, "MAX_CONNECTIONS", 1)
    e = Env(base, stub)
    e.write_registry()
    e.start()
    try:
        held = socket.socket(socket.AF_UNIX)
        held.connect(str(e.sock))  # takes the one slot
        for _ in range(100):
            if e.broker._slots._value == 0:
                break
            time.sleep(0.05)
        silent = socket.socket(socket.AF_UNIX)
        silent.connect(str(e.sock))  # no room, and it never sends: its busy reply waits up to a second for it
        time.sleep(0.2)
        held.close()
        for _ in range(100):
            if e.broker._slots._value == 1:
                break
            time.sleep(0.05)
        t0 = time.monotonic()
        assert broker.status(e.sock)["problems"] is not None
        assert time.monotonic() - t0 < 0.7  # it used to wait out the silent one
        silent.close()
    finally:
        e.broker.stop()


# ---------------------------------------------------------------- run records, tiers, areas and the kill switch


class Scoped:
    """A broker with a run record folder and a kill switch, over stub secrets."""

    def __init__(self, base: Path, stub: Stub):
        self.stub, self.s = stub, StubSecrets(base)
        self.host = stub.host
        self.log = base / "logs" / "broker.jsonl"
        self.sock = base / "run" / "b.sock"
        self.s.register("open", [self.host])  # unscoped: the older rule
        self.s.register("work", [self.host], tier="worker", area="home")
        self.s.register("leadkey", [self.host], tier="lead", area="home")
        self.s.register("rootish", [self.host], tier="lead-elevated", area="home")
        self.s.register("walledkey", [self.host], tier="walled", area="fiu")
        self.s.register("otherarea", [self.host], tier="worker", area="money")
        self.broker: broker.Broker | None = None

    def start(self, **kw) -> broker.Broker:
        self.broker = broker.Broker(
            self.sock,
            self.s.registry,
            self.s.store,
            log_path=self.log,
            allow_plain_http=True,
            records=self.s.records,
            elevate_file=self.s.elevate_file,
            **kw,
        ).start()
        return self.broker

    def lease(self, name: str, run: str | None = None, **extra) -> broker.Lease:
        if extra:  # a hand-made request, to plant claims the client function never sends
            reply = broker._client_call(
                {"op": "request", "name": name, "purpose": "run", "host": self.host, "run": run, **extra}, self.sock
            )
            r = reply["lease"]
            return broker.Lease(r["lease"], r["base_url"], r["header"], r["expires_at"])
        return broker.request(name, "run", self.host, socket=self.sock, run=run)

    def get(self, lease: broker.Lease) -> int:
        hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
        c = http.client.HTTPConnection(hostport, timeout=10)
        c.request("GET", "/" + prefix + "/v1/x")
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def audit(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def raw_log(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def wait_for(self, status: str, within: float = 5.0) -> bool:
        end = time.monotonic() + within
        while time.monotonic() < end:
            if any(a["status"] == status for a in self.audit()):
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def scoped(base, stub):
    sc = Scoped(base, stub)
    sc.start()
    yield sc
    sc.broker.stop()


def test_tier_a_name_outside_the_run_records_tier_is_refused_with_one_audit_line_and_no_value(scoped):
    scoped.s.record("r-worker", "home", "worker", ["work", "leadkey"])
    before = len(scoped.audit())
    e = refusal(scoped.lease, "leadkey", run="r-worker")
    assert e.code == "tier_refused"
    lines = scoped.audit()[before:]
    assert len(lines) == 1, lines
    assert lines[0]["status"] == "tier_refused" and lines[0]["name"] == "leadkey"
    assert lines[0]["run"] == "r-worker" and lines[0]["tier"] == "worker" and lines[0]["area"] == "home"
    assert scoped.s.leaked(scoped.raw_log() + str(e)) == []
    assert scoped.get(scoped.lease("work", run="r-worker")) == 200  # the name its tier reaches is fine


def test_tier_reach_lead_reaches_worker_and_walled_reaches_only_walled(scoped):
    scoped.s.record("r-lead", "home", "lead", ["work", "leadkey", "rootish"])
    assert scoped.get(scoped.lease("work", run="r-lead")) == 200
    assert scoped.get(scoped.lease("leadkey", run="r-lead")) == 200
    assert refusal(scoped.lease, "rootish", run="r-lead").code == "tier_refused"
    scoped.s.record("r-wall", "fiu", "walled", ["walledkey", "open"])
    assert scoped.get(scoped.lease("walledkey", run="r-wall")) == 200
    assert refusal(scoped.lease, "open", run="r-wall").code == "tier_refused", "an unscoped name is worker tier"
    scoped.s.record("r-up", "fiu", "lead-elevated", ["walledkey"])
    assert refusal(scoped.lease, "walledkey", run="r-up").code == "tier_refused", "nothing reaches into walled"


def test_tier_a_forged_request_claim_is_refused(scoped):
    scoped.s.record("r-worker", "home", "worker", ["work", "leadkey"])
    e = refusal(scoped.lease, "leadkey", run="r-worker", tier="lead")
    assert e.code == "claim_refused"
    assert refusal(scoped.lease, "work", run="r-worker", area="money").code == "claim_refused"
    assert refusal(scoped.lease, "open", run=None, tier="lead").code == "claim_refused"
    assert scoped.get(scoped.lease("work", run="r-worker", tier="worker", area="home")) == 200, "a true claim is fine"
    assert [a["status"] for a in scoped.audit()].count("claim_refused") == 3


def test_run_record_an_undeclared_name_with_a_valid_run_is_refused(scoped):
    scoped.s.record("r1", "home", "lead", ["work"])
    assert refusal(scoped.lease, "leadkey", run="r1").code == "not_declared"
    assert refusal(scoped.lease, "open", run="r1").code == "not_declared", "an unscoped name too, once a run is named"


def test_run_record_no_run_id_is_refused_for_a_scoped_name_and_with_require_run(scoped, base, stub):
    assert refusal(scoped.lease, "work").code == "run_required"
    assert scoped.lease("open").header == "x-api-key", "unscoped names keep the older rule by default"
    scoped.broker.stop()
    scoped.start(require_run=True)
    assert refusal(scoped.lease, "open").code == "run_required"
    assert scoped.broker._op_status()["require_run"] is True


def test_run_record_area_b_asking_for_area_a_is_refused_with_one_line(scoped):
    scoped.s.record("r-b", "home", "worker", ["otherarea"])
    before = len(scoped.audit())
    assert refusal(scoped.lease, "otherarea", run="r-b").code == "area_refused"
    lines = scoped.audit()[before:]
    assert [x["status"] for x in lines] == ["area_refused"]


def test_run_record_unknown_loose_symlinked_or_wrong_records_are_refused(scoped):
    assert refusal(scoped.lease, "work", run="nosuch").code == "run_refused"
    assert refusal(scoped.lease, "work", run="../escape").code == "run_refused"
    scoped.s.record("loose", "home", "worker", ["work"])
    (scoped.s.records / "loose").chmod(0o644)
    assert "untrusted" in refusal(scoped.lease, "work", run="loose").reason
    scoped.s.record("real", "home", "lead", ["leadkey"])
    (scoped.s.records / "link").symlink_to(scoped.s.records / "real")
    assert refusal(scoped.lease, "leadkey", run="link").code == "run_refused"
    (scoped.s.records / "other").write_text(
        json.dumps({"v": 1, "run": "real", "area": "home", "tier": "lead", "names": []})
    )
    (scoped.s.records / "other").chmod(0o600)
    assert "another run" in refusal(scoped.lease, "leadkey", run="other").reason
    (scoped.s.records / "extra").write_text(
        json.dumps({"v": 1, "run": "extra", "area": "home", "tier": "lead", "names": ["leadkey"], "admin": True})
    )
    (scoped.s.records / "extra").chmod(0o600)
    assert refusal(scoped.lease, "leadkey", run="extra").code == "run_refused", "an unknown key is not ignored"
    scoped.s.record("ok", "home", "worker", ["work"])
    scoped.s.records.chmod(0o755)
    try:
        assert refusal(scoped.lease, "work", run="ok").code == "run_refused", "a loose folder is not trusted"
    finally:
        scoped.s.records.chmod(0o700)


def test_run_record_the_lease_ends_with_the_run(scoped):
    scoped.s.record("r1", "home", "worker", ["work"])
    lease = scoped.lease("work", run="r1")
    assert scoped.get(lease) == 200
    assert broker.remove_run_record(scoped.s.records, "r1") is True
    assert scoped.get(lease) == 403
    assert scoped.wait_for("run_ended"), "the proxy writes its line after the reply, so wait for it"
    scoped.s.record("r2", "home", "worker", ["work"], expires_at=time.time() + 30)
    lease2 = scoped.lease("work", run="r2")
    assert datetime_from(lease2.expires_at) <= time.time() + 31, "a lease never outlives its run"
    scoped.s.record("r3", "home", "worker", ["work"], expires_at=time.time() - 1)
    assert "ended" in refusal(scoped.lease, "work", run="r3").reason


def datetime_from(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def test_run_record_a_caller_the_record_does_not_name_is_refused(scoped):
    scoped.s.record("r1", "home", "worker", ["work"], uid=os.geteuid() + 1)
    assert refusal(scoped.lease, "work", run="r1").code == "caller_refused"
    scoped.s.record("r2", "home", "worker", ["work"], uid=os.geteuid())
    assert scoped.get(scoped.lease("work", run="r2")) == 200


def test_run_record_a_grant_whose_audit_line_cannot_be_written_is_refused(scoped):
    scoped.s.record("r1", "home", "worker", ["work"])
    scoped.log.unlink(missing_ok=True)
    scoped.log.mkdir(parents=True)  # a folder where the log should be: every write fails
    e = refusal(scoped.lease, "work", run="r1")
    assert e.code == "audit_failed"
    assert scoped.broker._grants == {}


def test_elevate_off_refuses_elevated_leases_drops_live_ones_and_revokes(scoped):
    scoped.s.record("r-el", "home", "lead-elevated", ["rootish", "leadkey"])
    revoked: list[str] = []
    scoped.broker.revokers.append(lambda g: revoked.append(g.name) or True)
    live = scoped.lease("rootish", run="r-el")
    other = scoped.lease("leadkey", run="r-el")
    assert scoped.get(live) == 200
    out = broker.elevate(False, scoped.sock)
    assert out == {"elevated": False, "saved": True, "dropped": 1, "revoked": 1, "revoke_failed": 0}
    assert revoked == ["rootish"]
    assert scoped.get(live) == 403, "the live elevated lease is gone at once"
    assert scoped.get(other) == 200, "a lead name is not elevated"
    e = refusal(scoped.lease, "rootish", run="r-el")
    assert e.code == "elevation_off"
    assert [a["status"] for a in scoped.audit() if a["op"] in ("elevate", "revoke")] == ["ok", "off"]
    assert scoped.s.leaked(scoped.raw_log()) == []
    assert broker.status(scoped.sock)["elevated"] is False
    assert broker.elevate(True, scoped.sock)["elevated"] is True
    assert scoped.get(scoped.lease("rootish", run="r-el")) == 200


def test_elevate_a_failing_revoker_is_counted_and_the_lease_still_dropped(scoped):
    scoped.s.record("r-el", "home", "lead-elevated", ["rootish"])

    def boom(g):
        raise RuntimeError("upstream down")

    scoped.broker.revokers.append(boom)
    live = scoped.lease("rootish", run="r-el")
    assert broker.elevate(False, scoped.sock)["revoke_failed"] == 1
    assert scoped.get(live) == 403
    assert any(a["op"] == "revoke" and a["status"] == "failed" for a in scoped.audit())


def test_elevate_an_unreadable_or_foreign_switch_file_counts_as_off(scoped):
    scoped.s.record("r-el", "home", "lead-elevated", ["rootish"])
    scoped.s.elevate_file.write_text("not json")
    assert refusal(scoped.lease, "rootish", run="r-el").code == "elevation_off"
    scoped.s.elevate_file.write_text(json.dumps({"elevated": "on"}))
    scoped.s.elevate_file.chmod(0o666)
    assert refusal(scoped.lease, "rootish", run="r-el").code == "elevation_off", "a file others can write is off"
    scoped.s.elevate_file.chmod(0o600)
    assert scoped.get(scoped.lease("rootish", run="r-el")) == 200


def test_elevate_on_is_only_for_the_brokers_own_user(scoped):
    b = scoped.broker
    r = b.dispatch(json.dumps({"op": "elevate", "state": "off"}).encode(), b.own_uid + 1, 1)
    assert r["ok"] is True, "anyone allowed may pull the switch"
    r = b.dispatch(json.dumps({"op": "elevate", "state": "on"}).encode(), b.own_uid + 1, 1)
    assert r == {"ok": False, "code": "caller_refused", "reason": "only the broker's own user turns elevation back on"}
    assert b.elevation_on() is False


def test_elevate_cli_with_no_broker_sets_the_file_and_on_is_refused_in_a_session(base, monkeypatch, capsys):
    from ws import agentsession, cli

    monkeypatch.setenv("WS_HOME", str(base))
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    ns = cli.build_parser().parse_args(["elevate", "off"])
    assert broker.cmd_elevate(ns) == 0
    assert broker.read_elevation(broker.elevate_path(), os.geteuid()) is False
    assert "no broker running" in capsys.readouterr().out
    monkeypatch.setenv(agentsession.ENV_MARKER, "1")
    with pytest.raises(broker.WsError) as e:
        broker.cmd_elevate(cli.build_parser().parse_args(["elevate", "on"]))
    assert e.value.exit_code == 3
    assert broker.cmd_elevate(cli.build_parser().parse_args(["elevate", "off"])) == 0, "off works from a session"


def test_run_record_cli_writes_shows_and_removes(base, monkeypatch, capsys):
    from ws import cli

    monkeypatch.setenv("WS_HOME", str(base))
    monkeypatch.setenv("HOME", str(base))
    p = cli.build_parser()
    argv = ["broker", "run-record", "write", "r9", "--area", "home", "--tier", "worker", "--name", "work"]
    assert broker.cmd_run_record(p.parse_args([*argv, "--name", "work", "--ttl", "60"])) == 0
    rec = broker.read_run_record(broker.records_dir(), "r9", os.geteuid())
    assert rec.names == ("work",) and rec.tier == "worker" and rec.expires_at is not None
    assert (broker.records_dir() / "r9").stat().st_mode & 0o777 == 0o600
    assert broker.records_dir().stat().st_mode & 0o777 == 0o700
    assert broker.cmd_run_record(p.parse_args(["broker", "run-record", "show", "r9"])) == 0
    assert '"tier": "worker"' in capsys.readouterr().out
    assert broker.cmd_run_record(p.parse_args(["broker", "run-record", "rm", "r9"])) == 0
    with pytest.raises(broker.WsError):
        broker.cmd_run_record(p.parse_args(["broker", "run-record", "show", "r9"]))
    with pytest.raises(broker.WsError):
        p.parse_args([*argv[:6], "--tier", "god"])


def test_audit_copy_to_a_folder_and_to_an_scp_target(base):
    log = base / "broker.jsonl"
    log.write_text('{"op": "request"}\n')
    dest = base / "offbox"
    dest.mkdir()
    assert broker.audit_copy(log, str(dest), "my box") == log.stat().st_size
    assert (dest / "broker-my-box.jsonl").read_text() == log.read_text()
    assert (dest / "broker-my-box.jsonl").stat().st_mode & 0o777 == 0o600
    seen = []

    class Done:
        returncode = 0

    broker.audit_copy(log, "store.example:logs", "box", run=lambda argv, **k: seen.append(argv) or Done())
    assert seen[0][:5] == ["scp", "-q", "-B", "-o", "BatchMode=yes"]
    assert seen[0][-1] == "store.example:logs/broker-box.jsonl"
    with pytest.raises(broker.WsError):
        broker.audit_copy(log, str(base / "missing"), "box")


# ---------------------------------------------------------------- cold review: the kill switch never fails open


def test_elevate_must1_with_no_broker_it_refuses_rather_than_write_a_switch_the_broker_never_reads(base, monkeypatch):
    from ws import cli

    monkeypatch.setenv("WS_HOME", str(base))
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("WS_BROKER_SOCKET", str(base / "none.sock"))
    with pytest.raises(broker.WsError) as e:
        broker.cmd_elevate(cli.build_parser().parse_args(["elevate", "off"]))
    assert e.value.kind == "unavailable" and "not this user's to write" in e.value.message
    assert "sudo -u ws-broker" in e.value.hint
    assert not broker.elevate_path().exists(), "no switch written where the broker would not look"
    monkeypatch.delenv("WS_BROKER_SOCKET")
    store = wsecrets.store_dir()
    store.mkdir(parents=True, mode=0o700)
    real = os.geteuid
    monkeypatch.setattr(broker.os, "geteuid", lambda: real() + 1)  # the store now reads as another user's: walled
    assert "belongs to another user" in broker._not_this_users_broker(None)


def test_elevate_must2_a_switch_file_that_cannot_be_written_still_turns_elevation_off(scoped):
    scoped.s.record("r-el", "home", "lead-elevated", ["rootish"])
    live = scoped.lease("rootish", run="r-el")
    scoped.s.elevate_file.mkdir()  # a folder where the file should be: every write fails
    out = broker.elevate(False, scoped.sock)
    assert out["saved"] is False and out["dropped"] == 1 and out["elevated"] is False
    assert scoped.get(live) == 403
    assert refusal(scoped.lease, "rootish", run="r-el").code == "elevation_off"
    assert broker.status(scoped.sock)["elevated"] is False
    assert "off_unsaved" in [a["status"] for a in scoped.audit() if a["op"] == "elevate"]
    e = refusal(broker.elevate, True, scoped.sock)
    assert e.code == "store_unreadable", "on needs the file; it stays off"
    assert scoped.broker.elevation_on() is False


def test_elevate_must2_two_writes_at_once_never_share_a_temp_file(base):
    import concurrent.futures

    f = base / "elevate.json"
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: broker.write_elevation(f, i % 2 == 0), range(64)))
    assert broker.read_elevation(f, os.geteuid()) in (True, False)
    assert [p.name for p in base.iterdir()] == ["elevate.json"]


def test_elevate_on_refuses_when_its_audit_line_cannot_be_written(scoped):
    broker.elevate(False, scoped.sock)
    scoped.log.unlink()
    scoped.log.mkdir()
    assert refusal(broker.elevate, True, scoped.sock).code == "audit_failed"
    assert scoped.broker.elevation_on() is False


def test_elevate_a_name_retiered_to_elevated_after_its_lease_is_still_stopped(scoped):
    scoped.s.record("r-el", "home", "lead-elevated", ["leadkey"])
    live = scoped.lease("leadkey", run="r-el")
    text = scoped.s.registry.read_text()
    marked = f'[leadkey]\nhosts = ["{scoped.host}"]\ntier = "lead"'
    assert marked in text
    scoped.s.registry.write_text(text.replace(marked, marked.replace('"lead"', '"lead-elevated"')))
    assert broker.elevate(False, scoped.sock)["dropped"] == 1, "the registry's tier now, not the grant's"
    assert scoped.get(live) == 403


def test_run_record_rewritten_with_another_area_tier_or_uid_ends_the_leases_under_it(scoped):
    for change in ({"area": "money"}, {"tier": "lead"}, {"uid": os.geteuid() + 1}):
        scoped.s.record("r1", "home", "worker", ["work"])
        live = scoped.lease("work", run="r1")
        assert scoped.get(live) == 200
        kw = {"area": "home", "tier": "worker"} | change
        scoped.s.record("r1", kw.pop("area"), kw.pop("tier"), ["work"], **kw)
        assert scoped.get(live) == 403, change


def test_run_record_a_symlinked_record_folder_is_refused(scoped, base):
    scoped.s.record("r1", "home", "worker", ["work"])
    real = base / "elsewhere"
    scoped.s.records.rename(real)
    scoped.s.records.symlink_to(real)
    e = refusal(scoped.lease, "work", run="r1")
    assert e.code == "run_refused" and "symlink" in e.reason


# -- per-secret lease limit and lease count in the audit line (F-B10)


def _ttl_registry(env, ttl: str) -> None:
    env.write_registry(f'[anthropic]\nhosts = ["{env.stub.host}"]\nttl = {ttl}\n')


def _expires_in(env, lease) -> float:
    import calendar

    return calendar.timegm(time.strptime(lease.expires_at, "%Y-%m-%dT%H:%M:%SZ")) - env.now


def test_a_secrets_own_ttl_caps_its_lease_and_a_longer_ask_cannot_beat_it(env):
    _ttl_registry(env, "120")
    assert 118 <= _expires_in(env, env.lease()) <= 121  # no ask: the secret's own limit
    assert 118 <= _expires_in(env, env.lease(ttl_s=3000)) <= 121  # a longer ask is cut to it
    assert 28 <= _expires_in(env, env.lease(ttl_s=30)) <= 31  # a shorter ask is kept


def test_a_secret_without_ttl_gets_the_brokers_limit_which_is_one_hour_by_default(env):
    assert broker.DEFAULT_TTL_S == 3600
    assert 3598 <= _expires_in(env, env.lease()) <= 3601


@pytest.mark.parametrize("bad", ["0", "-5", '"60"', "true", "86401"])
def test_a_bad_ttl_in_the_registry_refuses_the_whole_registry(env, bad):
    _ttl_registry(env, bad)
    with pytest.raises(wsecrets.SecretsError, match="ttl"):
        wsecrets.load_registry(env.registry)


def test_the_request_audit_line_counts_that_names_open_leases(env):
    for _ in range(3):
        env.lease()
    env.lease(name="github")
    ok = [a for a in env.audit() if a["op"] == "request" and a["status"] == "ok"]
    assert [a["leases"] for a in ok if a["name"] == "anthropic"] == [1, 2, 3]
    assert [a["leases"] for a in ok if a["name"] == "github"] == [1]
    assert VALUE not in env.log.read_text()


# -- a path allow list per name (B3), and the stored value's repr (rr-M5)


def _paths_registry(env, allowed: str) -> None:
    env.write_registry(f'[anthropic]\nhosts = ["{env.stub.host}"]\npaths = {allowed}\n')


def test_a_name_with_paths_forwards_only_those_and_the_stub_never_sees_the_rest(env):
    _paths_registry(env, '["/v1/messages", "/v1/models/"]')
    lease = env.lease()
    assert env.call("/v1/messages", lease=lease)[0] == 200
    assert env.call("/v1/messages/batch", lease=lease)[0] == 200  # inside the prefix
    assert env.call("/v1/models/x", lease=lease)[0] == 200
    seen = len(env.stub.seen)
    for bad in ("/v1/other", "/v1/messagesX", "/", "/v1/../etc", "/v1/messages/%2e%2e/admin", "/v1/messages%2fx"):
        assert env.call(bad, lease=lease)[0] == 403, bad
    assert len(env.stub.seen) == seen  # a refused path never reached the upstream
    end = time.monotonic() + 5
    while time.monotonic() < end and not any(a["status"] == "path_not_allowed" for a in env.audit()):
        time.sleep(0.02)
    assert any(a["op"] == "proxy" and a["status"] == "path_not_allowed" for a in env.audit())


def test_a_name_without_paths_forwards_any_path(env):
    assert env.call("/anything/at/all")[0] == 200


@pytest.mark.parametrize(
    "bad", ["[]", '["v1"]', '["/v1?x=1"]', '["/v1/../x"]', '["/%2e"]', '["/a b"]', '"/v1"', '["/v1\\n"]']
)
def test_a_bad_paths_entry_refuses_the_whole_registry(env, bad):
    _paths_registry(env, bad)
    with pytest.raises(wsecrets.SecretsError, match="paths|path prefix|list"):
        wsecrets.load_registry(env.registry)


@pytest.mark.parametrize(
    "tricky",
    [
        "/v1/messages/..;/admin",
        "/v1/messages/%2e%2e;/admin",
        "/v1/messages/%2E%2E%3B/x",
        "/v1/messages/.%00./x",
        "/v1/messages/%c0%ae%c0%ae/x",
        "/v1/messages;x",
        "/v1/messages/a%20b",
        "/v1/messages/a\\b",
    ],
)
def test_a_server_reading_a_path_trick_as_dot_dot_gets_nothing_when_paths_is_set(env, tricky):
    _paths_registry(env, '["/v1/messages"]')
    lease = env.lease()
    seen = len(env.stub.seen)
    assert env.call(tricky, lease=lease)[0] == 403
    assert len(env.stub.seen) == seen


def _base_registry(env, extra: str) -> None:
    env.write_registry(f'[anthropic]\nhosts = ["{env.stub.host}"]\n{extra}\n')


def test_a_base_path_goes_in_front_of_the_request_path_and_the_query(env):
    _base_registry(env, 'base_path = "/api"')
    assert env.call("/v1/messages?beta=1")[0] == 200
    assert env.stub.seen[-1]["target"] == "/api/v1/messages?beta=1"


def test_a_base_path_with_a_trailing_slash_is_the_same_as_without(env):
    _base_registry(env, 'base_path = "/api/"')
    assert env.call("/v1/messages")[0] == 200
    assert env.stub.seen[-1]["target"] == "/api/v1/messages"


def test_without_a_base_path_the_target_is_as_before(env):
    _base_registry(env, "")
    assert env.call("/v1/messages")[0] == 200
    assert env.stub.seen[-1]["target"] == "/v1/messages"


def test_paths_judges_the_path_the_tool_sent_not_the_one_with_the_prefix(env):
    _base_registry(env, 'base_path = "/api"\npaths = ["/v1/messages"]')
    lease = env.lease()
    assert env.call("/v1/messages", lease=lease)[0] == 200
    seen = len(env.stub.seen)
    assert env.call("/api/v1/messages", lease=lease)[0] == 403  # the prefix is not part of the allowed list
    assert len(env.stub.seen) == seen


@pytest.mark.parametrize("tricky", ["/../admin", "/v1/../../admin", "/v1/%2e%2e/admin", "/v1/a;b", "/v1/a\\b"])
def test_a_base_path_cannot_be_left_by_a_dot_dot_even_with_no_paths_list(env, tricky):
    _base_registry(env, 'base_path = "/api"')
    lease = env.lease()
    seen = len(env.stub.seen)
    assert env.call(tricky, lease=lease)[0] == 403
    assert len(env.stub.seen) == seen


def test_an_absolute_url_as_the_path_stays_under_the_base_path_or_is_refused(env):
    _base_registry(env, 'base_path = "/api"')
    lease = env.lease()
    seen = len(env.stub.seen)
    status = env.call("http://evil.example.com/", lease=lease)[0]
    forwarded = env.stub.seen[seen:]
    assert status == 404 and forwarded == []  # no lease token in the path: the broker answers itself


@pytest.mark.parametrize(
    "bad", ['""', '"api"', '"/a/../b"', '"//api"', '"/api//v1"', '"/api?x=1"', '"/a b"', "5", '"/%2e"', '"/api\\n"']
)
def test_a_bad_base_path_refuses_the_whole_registry(env, bad):
    _base_registry(env, f"base_path = {bad}")
    with pytest.raises(wsecrets.SecretsError, match="base_path"):
        wsecrets.load_registry(env.registry)


def test_tightening_paths_or_ttl_in_the_registry_bites_leases_already_open(env):
    lease = env.lease()
    assert env.call("/v1/other", lease=lease)[0] == 200  # no list yet
    _paths_registry(env, '["/v1/messages"]')
    assert env.call("/v1/other", lease=lease)[0] == 403
    assert env.call("/v1/messages", lease=lease)[0] == 200
    _ttl_registry(env, "100")
    env.now += 150  # the lease itself runs for an hour, the registry now says 100 s
    assert env.call("/v1/messages", lease=lease)[0] == 403
    end = time.monotonic() + 5  # the proxy answers first and writes its audit row after: wait for the row
    while time.monotonic() < end and not any(a["op"] == "proxy" and a["status"] == "expired" for a in env.audit()):
        time.sleep(0.02)
    assert any(a["op"] == "proxy" and a["status"] == "expired" for a in env.audit())


# -- B2: the registry is trusted only if it is the broker's user's (or root's) and closed to group and world writes


def _foreign_registry(monkeypatch, registry: Path) -> None:
    """Make the registry look owned by another uid to the check, without root: only its own fstat is changed."""
    ident = (registry.stat().st_dev, registry.stat().st_ino)
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        if (st.st_dev, st.st_ino) != ident:
            return st
        f = list(st)
        f[stat.ST_UID] = os.geteuid() + 12345
        return os.stat_result(f)

    monkeypatch.setattr(wsecrets.os, "fstat", fstat)


def test_registry_b2_a_group_writable_registry_is_refused_with_one_audit_line(env):
    env.registry.chmod(0o664)
    before = len(env.audit())
    e = refusal(env.lease)
    assert e.code == "registry_untrusted"
    lines = env.audit()[before:]
    assert [a["status"] for a in lines] == ["registry_untrusted"], lines
    assert VALUE not in env.log.read_text() + str(e)
    assert env.broker._grants == {}
    env.registry.chmod(0o666)
    assert refusal(env.lease).code == "registry_untrusted", "world writable too"
    env.registry.chmod(0o644)
    assert env.call()[0] == 200, "closed again, it is trusted again"


def test_registry_b2_a_registry_owned_by_another_user_is_refused_with_one_audit_line(env, monkeypatch):
    _foreign_registry(monkeypatch, env.registry)
    before = len(env.audit())
    assert refusal(env.lease).code == "registry_untrusted"
    assert [a["status"] for a in env.audit()[before:]] == ["registry_untrusted"]
    assert any("not the broker's user" in p for p in broker.status(env.sock)["problems"])
    monkeypatch.undo()
    assert env.call()[0] == 200


def test_registry_b2_an_open_lease_stops_when_the_registry_is_opened_to_others(env):
    lease = env.lease()
    assert env.call(lease=lease)[0] == 200
    seen = len(env.stub.seen)
    env.registry.chmod(0o664)
    status, _, body = env.call(lease=lease)
    assert status == 503 and b"registry_untrusted" in body
    assert len(env.stub.seen) == seen, "nothing was forwarded"
    env.registry.chmod(0o644)
    assert env.call(lease=lease)[0] == 200


def test_registry_b2_a_symlinked_registry_or_a_writable_folder_is_refused(env):
    real = env.base / "elsewhere.toml"
    env.registry.rename(real)
    env.registry.symlink_to(real)
    before = len(env.audit())
    assert refusal(env.lease).code == "registry_untrusted"
    assert [a["status"] for a in env.audit()[before:]] == ["registry_untrusted"]
    env.registry.unlink()
    real.rename(env.registry)
    folder = env.registry.parent
    mode = folder.stat().st_mode & 0o777
    folder.chmod(mode | 0o020)
    try:
        assert refusal(env.lease).code == "registry_untrusted"
    finally:
        folder.chmod(mode)
    assert env.call()[0] == 200


def test_registry_b2_minters_refuse_an_untrusted_registry(scoped):
    scoped.s.registry.chmod(0o664)
    before = len(scoped.audit())
    e = refusal(broker._client_call, {"op": "mint", "name": "work", "run": "r1"}, scoped.sock)
    assert e.code == "registry_untrusted"
    assert [a["status"] for a in scoped.audit()[before:]] == ["registry_untrusted"]


# -- a retry after a failed use-record (audit) write leaves the open leases and their counts as they were


def test_run_record_a_retry_after_a_failed_audit_write_leaves_open_leases_and_counts_as_they_were(scoped):
    scoped.s.record("r1", "home", "worker", ["work"])
    first = scoped.lease("work", run="r1")
    open_before = dict(scoped.broker._grants)
    leases_before = broker.status(scoped.sock)["leases"]
    scoped.log.rename(scoped.log.with_name("moved.jsonl"))
    scoped.log.mkdir()  # a folder where the log should be: the next write fails
    assert refusal(scoped.lease, "work", run="r1").code == "audit_failed"
    assert scoped.broker._grants == open_before, "the refused grant left nothing behind"
    assert broker.status(scoped.sock)["leases"] == leases_before
    scoped.log.rmdir()
    scoped.log.with_name("moved.jsonl").rename(scoped.log)
    second = scoped.lease("work", run="r1")
    assert second.lease != first.lease
    assert len(scoped.broker._grants) == len(open_before) + 1
    ok = [a for a in scoped.audit() if a["op"] == "request" and a["status"] == "ok" and a["name"] == "work"]
    assert [a["leases"] for a in ok] == [1, 2], "the failed attempt is not counted"
    assert scoped.get(first) == 200 and scoped.get(second) == 200


def test_a_minted_tokens_repr_shows_no_token():
    token = "planted-" + os.urandom(8).hex()
    m = broker._Minted(name="app", run="r1", elevated=False, token=token, options=None, expires=0.0)
    assert token not in repr(m) and token not in str(m)


# ---------------------------------------------------------------- a token in the path (Telegram's /bot<token>)

PATH_VALUE = "123456:AAH-x_y"


def _token_registry(env, extra: str = "") -> None:
    env.write_registry(
        f'[anthropic]\nhosts = ["{env.stub.host}"]\n\n'
        f'[tg]\nhosts = ["{env.stub.host}"]\nbase_path = "/bot{{value}}"\n{extra}\n'
    )
    env.store.write("tg", PATH_VALUE)


def _audit_has(env, status: str) -> bool:
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if any(a["op"] == "proxy" and a["status"] == status for a in env.audit()):
            return True
        time.sleep(0.02)
    return False


def test_a_path_token_entry_puts_the_value_in_the_path_and_in_no_header(env):
    _token_registry(env)
    lease = env.lease(name="tg")
    assert lease.header == ""
    status, _, _ = env.call("/sendMessage?chat_id=1", lease=lease, headers={"x-api-key": "placeholder"})
    assert status == 200
    seen = env.stub.seen[-1]
    assert seen["target"] == f"/bot{PATH_VALUE}/sendMessage?chat_id=1"
    assert not any(PATH_VALUE in v for v in seen["headers"].values())
    assert "x-api-key" not in seen["headers"]  # the tool's placeholder is still stripped


def test_paths_judges_the_client_path_of_a_path_token_entry(env):
    _token_registry(env, 'paths = ["/getMe", "/sendMessage"]')
    lease = env.lease(name="tg")
    assert env.call("/getMe", lease=lease)[0] == 200
    seen = len(env.stub.seen)
    assert env.call("/deleteWebhook", lease=lease)[0] == 403
    assert env.call(f"/bot{PATH_VALUE}/getMe", lease=lease)[0] == 403
    assert len(env.stub.seen) == seen


@pytest.mark.parametrize("bad", ["12/../x", "a.b", "a%2fb", "a?b", "a#b", "a b", "x" * 257])
def test_a_path_value_that_could_move_the_request_is_refused(env, bad):
    _token_registry(env)
    env.store.write("tg", bad)
    lease = env.lease(name="tg")
    seen = len(env.stub.seen)
    status, _, data = env.call("/getMe", lease=lease)
    assert status == 503 and json.loads(data) == {"error": "value_refused"}
    assert len(env.stub.seen) == seen
    assert _audit_has(env, "value_refused")
    assert bad not in env.log.read_text()


def test_a_redirect_from_a_path_token_host_is_refused_and_names_nothing(env):
    _token_registry(env)
    status, headers, data = env.call("/echo-loc", lease=env.lease(name="tg"))
    assert status == 502 and json.loads(data) == {"error": "redirect_refused"}
    assert not any(PATH_VALUE in v for v in headers.values())
    assert PATH_VALUE.encode() not in data


def test_reply_headers_holding_the_path_value_are_dropped(env):
    _token_registry(env)
    status, headers, _ = env.call("/echo-head", lease=env.lease(name="tg"))
    assert status == 200 and headers.get("X-Upstream") == "yes"
    low = {k.lower() for k in headers}
    assert "content-location" not in low and "x-echo-encoded" not in low
    assert not any(PATH_VALUE in v or PATH_VALUE.replace(":", "%3A") in v for v in headers.values())


def test_a_reply_body_holding_the_path_value_is_cut_before_it(env):
    _token_registry(env)
    lease = env.lease(name="tg")
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("GET", "/" + prefix + "/echo-body")
    r = c.getresponse()
    try:
        got = r.read()
    except http.client.IncompleteRead as e:
        got = e.partial
    c.close()
    assert PATH_VALUE.encode() not in got
    assert _audit_has(env, "secret_in_reply")


def test_the_path_value_never_reaches_the_audit_log(env):
    _token_registry(env)
    lease = env.lease(name="tg")
    for p in ("/getMe", "/echo-loc", "/echo-head", "/../x"):
        env.call(p, lease=lease)
    assert _audit_has(env, "redirect_refused")
    assert PATH_VALUE not in env.log.read_text()


def test_a_header_entry_still_relays_reply_headers_and_bodies_untouched(env):
    status, headers, _ = env.call("/echo-head")
    assert status == 200 and "Content-Location" in headers
    status, _, data = env.call("/echo-body")
    assert status == 200 and data.endswith(b'"}') and len(data) > 70000
    status, _, _ = env.call("/echo-loc")
    assert status == 302  # only a path-token entry refuses a redirect


@pytest.mark.parametrize(
    "bad",
    [
        'base_path = "/bot{value}/x"',
        'base_path = "/{value}{value}"',
        'base_path = "{value}"',
        'base_path = "/bot{val}"',
        'base_path = "/bot{value}"\nheader = "authorization"',
        'base_path = "/bot{value}"\nformat = "{value}"',
    ],
)
def test_a_misplaced_path_token_refuses_the_whole_registry(env, bad):
    env.write_registry(f'[tg]\nhosts = ["{env.stub.host}"]\n{bad}\n')
    with pytest.raises(wsecrets.SecretsError, match="base_path"):
        wsecrets.load_registry(env.registry)


def test_a_path_token_may_follow_a_folder(env):
    env.write_registry(f'[tg]\nhosts = ["{env.stub.host}"]\nbase_path = "/api/{{value}}"\n')
    entry = wsecrets.load_registry(env.registry)["tg"]
    assert entry.token_in_path and entry.header == "" and entry.base_path == "/api/{value}"


def test_a_clean_path_token_reply_arrives_byte_for_byte(env):
    _token_registry(env)
    status, _, data = env.call("/getMe", lease=env.lease(name="tg"))
    assert status == 200 and json.loads(data) == {"ok": True, "len": 0}
    status, _, data = env.call("/getMe", method="HEAD", lease=env.lease(name="tg"))
    assert status == 200 and data == b""
    assert env.call("/noreply", lease=env.lease(name="tg"))[0] == 204
    status, headers, _ = env.call("/notmod", lease=env.lease(name="tg"))
    assert status == 304 and headers.get("ETag") == '"1"'  # 304 is not a redirect


def test_a_path_token_request_asks_for_an_uncompressed_whole_reply(env):
    _token_registry(env)
    hdrs = {"Accept-Encoding": "gzip, br", "Range": "bytes=0-10", "If-Range": "x"}
    assert env.call("/getMe", headers=hdrs, lease=env.lease(name="tg"))[0] == 200
    seen = env.stub.seen[-1]["headers"]
    assert seen["accept-encoding"] == "identity" and "range" not in seen and "if-range" not in seen
    status, _, data = env.call("/gzip", lease=env.lease(name="tg"))
    assert status == 502 and json.loads(data) == {"error": "encoded_reply_refused"}
    env.call("/getMe", headers=hdrs)  # a header entry passes them on as before
    assert env.stub.seen[-1]["headers"]["range"] == "bytes=0-10"


def _raw_get(lease, path):
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("GET", "/" + prefix + path)
    r = c.getresponse()
    try:
        got = r.read()
    except http.client.IncompleteRead as e:
        got = e.partial
    c.close()
    return r, got


@pytest.mark.parametrize("route", ["/echo-split", "/echo-part"])
def test_a_value_split_across_reads_or_cut_short_is_still_caught(env, route):
    _token_registry(env)
    _r, got = _raw_get(env.lease(name="tg"), route)
    assert PATH_VALUE[: broker.SECRET_WINDOW].encode() not in got
    assert PATH_VALUE[-broker.SECRET_WINDOW :].encode() not in got or len(PATH_VALUE) <= broker.SECRET_WINDOW
    assert _audit_has(env, "secret_in_reply")


def test_a_long_value_is_caught_by_any_window_of_it(env):
    long_value = "1234567890:" + "AbC-dEf_GhI" * 3
    _token_registry(env)
    env.store.write("tg", long_value)
    _r, got = _raw_get(env.lease(name="tg"), "/echo-part")  # echoes 20 of its 44 characters, never the whole value
    assert got == b""  # the 24-byte reply holds a 16-character piece, so none of it is sent
    assert _audit_has(env, "secret_in_reply")
    status, _, data = env.call("/getMe", lease=env.lease(name="tg"))
    assert status == 200 and json.loads(data) == {"ok": True, "len": 0}  # a clean reply still flows


def test_the_status_phrase_of_a_path_token_reply_is_the_standard_one(env):
    _token_registry(env)
    r, _ = _raw_get(env.lease(name="tg"), "/echo-reason")
    assert r.status == 404 and r.reason == "Not Found"
    r, _ = _raw_get(env.lease(), "/echo-reason")
    assert r.reason.startswith("No route")  # a header entry keeps the upstream's phrase


def test_a_stream_through_a_path_token_lease_arrives_whole(env):
    _token_registry(env)
    lease = env.lease(name="tg")
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("GET", "/" + prefix + "/sse")
    r = c.getresponse()
    env.stub.go.set()  # the last bytes of each read wait for the next one, so the stream ends before it is read whole
    assert r.read() == b"data: one\n\ndata: two\n\n"
    c.close()
