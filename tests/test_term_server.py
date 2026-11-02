"""The web terminal's server: who may reach it, what the page carries, and the socket from handshake to hang-up."""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import struct
import threading
import time

import pytest

from ws.term import app, assets, bridge, page
from ws.term import websocket as w
from ws.term.testing import FakeChild
from ws.ui import server as ui_server

KEY = base64.b64encode(b"the sample nonce").decode()


class Conn:
    """A raw WebSocket client: sends the upgrade by hand and speaks masked frames."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.rf = self.sock.makefile("rb")
        self.status = ""
        self.headers: dict[str, str] = {}

    def upgrade(self, path: str, *, host: str, origin: str | None, key: str = KEY, version: str = "13") -> str:
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade"]
        lines += [f"Sec-WebSocket-Key: {key}", f"Sec-WebSocket-Version: {version}"]
        if origin is not None:
            lines.append(f"Origin: {origin}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.status = self.rf.readline().decode().strip()
        while (line := self.rf.readline().decode().strip()) != "":
            k, _, v = line.partition(":")
            self.headers[k.strip().lower()] = v.strip()
        return self.status

    def send(self, opcode: int, payload: bytes = b"") -> None:
        self.sock.sendall(w.encode_frame(opcode, payload, mask=True))

    def send_json(self, doc: dict) -> None:
        self.send(w.TEXT, json.dumps(doc).encode())

    def frame(self) -> tuple[int, bytes] | None:
        head = self.rf.read(2)
        if len(head) < 2:
            return None
        opcode, n = head[0] & 0x0F, head[1] & 0x7F
        if n == 126:
            (n,) = struct.unpack(">H", self.rf.read(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", self.rf.read(8))
        return opcode, self.rf.read(n)

    def close(self) -> None:
        self.rf.close()  # the buffered reader holds a second reference to the socket
        self.sock.close()


class Running:
    def __init__(self, **kw):
        self.children: list[FakeChild] = []
        self.session = bridge.Session(self._spawn, on_exit=lambda code: None)
        self.server = app.build_server(self.session, **kw)
        self.port, self.token = self.server.port, self.server.token
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()

    def _spawn(self, cols: int, rows: int) -> FakeChild:
        self.children.append(FakeChild(echo=True))
        return self.children[-1]

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def open(self, *, token: str | None = None, **over) -> Conn:
        c = Conn(self.port)
        args = {"host": self.host, "origin": self.origin}
        args.update(over)
        c.upgrade(f"{page.SOCKET_PATH}?t={token or self.token}", **args)
        return c

    def connect(self, cols: int = 90, rows: int = 25) -> Conn:
        c = self.open()
        assert c.status.startswith("HTTP/1.1 101"), c.status
        c.send_json({"type": "resize", "cols": cols, "rows": rows})
        return c

    def get(self, path: str, *, host: str | None = None, token: bool = True, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        q = f"?t={self.token}" if token else ""
        conn.putrequest("GET", path + q, skip_host=True)
        conn.putheader("Host", host or self.host)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def stop(self) -> None:
        self.session.close()
        self.server.stop()
        self.thread.join(5)


@pytest.fixture
def run():
    made: list[Running] = []

    def start(**kw) -> Running:
        made.append(Running(**kw))
        return made[-1]

    yield start
    for r in made:
        r.stop()


def until(cond, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


# -- the page and its script


def test_the_page_needs_the_token_and_the_right_host(run):
    r = run()
    assert r.get("/", token=False)[0].status == 403
    assert r.get("/", host="evil.example")[0].status == 403, "a DNS-rebinding request names another host"
    assert r.get("/", host=f"127.0.0.1:{r.port + 1}")[0].status == 403
    resp, body = r.get("/")
    assert resp.status == 200 and b"<title>ws term</title>" in body


def test_a_wrong_token_is_refused(run):
    r = run()
    conn = http.client.HTTPConnection("127.0.0.1", r.port, timeout=5)
    conn.request("GET", "/?t=wrong")
    assert conn.getresponse().status == 403


def test_the_page_policy_allows_the_vendored_script_by_hash_and_nothing_loose(run):
    r = run()
    resp, body = r.get("/")
    csp = resp.getheader("Content-Security-Policy")
    integrity = page.script_integrity(assets.bundle())
    nonce = re.search(r"nonce-([\w-]+)", csp).group(1)
    assert f"script-src '{integrity}' 'nonce-{nonce}'" in csp and "unsafe-eval" not in csp
    assert csp.count("unsafe-inline") == 1 and "style-src-attr 'unsafe-inline'" in csp
    html = body.decode()
    assert f'integrity="{integrity}"' in html and html.count(f'nonce="{nonce}"') == 2, "one style, one script"
    assert re.search(r"<script(?![^>]*(?:nonce|src|type=\"application/json\"))", html) is None, "no unmarked script"
    assert "on" + "click=" not in html and "javascript:" not in html


def test_the_page_has_the_phone_viewport_and_every_control_has_an_action(run):
    html = run().get("/")[1].decode()
    assert 'content="width=device-width, initial-scale=1, interactive-widget=resizes-visual"' in html
    for tag in re.findall(r"<button\b[^>]*>", html):
        assert "data-action=" in tag, tag


def test_the_script_is_served_by_token_and_is_the_pinned_bundle(run):
    r = run()
    resp, body = r.get(page.ASSET_PATH)
    assert (
        resp.status == 200 and body == assets.bundle() and resp.getheader("Content-Type").startswith("text/javascript")
    )
    assert r.get(page.ASSET_PATH, token=False)[0].status == 403


def test_no_response_carries_a_cors_header(run):
    r = run()
    for path in ("/", page.ASSET_PATH, "/nope"):
        resp, _ = r.get(path, headers={"Origin": "http://evil.example"})
        assert not [h for h, _ in resp.getheaders() if h.lower().startswith("access-control")], path
    forbidden, _ = r.get("/", token=False, headers={"Origin": "http://evil.example"})
    assert not [h for h, _ in forbidden.getheaders() if h.lower().startswith("access-control")]


def test_the_page_carries_no_secret_or_environment_value(run, monkeypatch):
    planted = "planted-" + "value-" + base64.b16encode(os.urandom(8)).decode()
    monkeypatch.setenv("WS_TERM_PLANTED", planted)
    monkeypatch.setenv("SOME_API_KEY", planted + "-key")
    r = run()
    resp, html = r.get("/")
    script = r.get(page.ASSET_PATH)[1]
    for text in (html, script, json.dumps(dict(resp.getheaders())).encode()):
        assert planted.encode() not in text
    cfg = json.loads(re.search(rb'id="cfg">(.*?)</script>', html, re.DOTALL).group(1))
    assert set(cfg) == {
        "keybar",
        "ctrl_punct",
        "shift_enter",
        "socket",
        "debounce_ms",
        "max_tries",
        "reset",
        "font",
        "themes",
    }


def test_each_launch_has_its_own_token_and_the_link_dies_with_the_server(run):
    a, b = run(), run()
    assert a.token != b.token and len(a.token) >= 20
    assert a.get("/")[0].status == 200
    conn = http.client.HTTPConnection("127.0.0.1", b.port, timeout=5)
    conn.request("GET", f"/?t={a.token}")
    assert conn.getresponse().status == 403, "another launch's token is no good"
    a.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", a.port), timeout=1)


def test_the_server_listens_on_loopback_only_by_default(run):
    r = run()
    assert r.server.httpd.socket.getsockname()[0] == "127.0.0.1" and r.server.bind == "127.0.0.1"
    assert r.server.url.startswith("http://127.0.0.1:")


# -- the socket: who may open it


def test_a_good_handshake_is_accepted_with_the_right_accept_value(run):
    r = run()
    c = r.open()
    assert c.status.startswith("HTTP/1.1 101")
    assert c.headers["sec-websocket-accept"] == w.accept_key(KEY)
    assert c.headers["upgrade"].lower() == "websocket"
    assert not [h for h in c.headers if h.startswith("access-control")]
    c.close()


@pytest.mark.parametrize(
    ("what", "over", "token"),
    [
        ("another site's origin", {"origin": "http://evil.example"}, None),
        ("an origin on another port", {"origin": "http://127.0.0.1:1"}, None),
        ("an https origin for a plain server", {"origin": "https://127.0.0.1:PORT"}, None),
        ("no origin at all", {"origin": None}, None),
        ("another host (DNS rebinding)", {"host": "evil.example"}, None),
        ("a host with another port", {"host": "127.0.0.1:1"}, None),
        ("no token", {}, ""),
        ("a wrong token", {}, "x" * 22),
    ],
)
def test_the_socket_is_refused_for(run, what, over, token):
    r = run()
    over = {k: (v.replace("PORT", str(r.port)) if isinstance(v, str) else v) for k, v in over.items()}
    c = Conn(r.port)
    path = page.SOCKET_PATH + (f"?t={token}" if token is not None else f"?t={r.token}")
    if token == "":
        path = page.SOCKET_PATH
    args = {"host": r.host, "origin": r.origin}
    args.update(over)
    status = c.upgrade(path, **args)
    assert status.startswith(("HTTP/1.0 403", "HTTP/1.1 403")), (what, status)
    assert r.children == [], "nothing was started"
    c.close()


@pytest.mark.parametrize("key", ["not base64!", "c2hvcnQ=", ""], ids=["not-base64", "too-short", "empty"])
def test_a_bad_key_is_a_400_and_starts_nothing(run, key):
    r = run()
    c = Conn(r.port)
    status = c.upgrade(f"{page.SOCKET_PATH}?t={r.token}", host=r.host, origin=r.origin, key=key)
    assert " 400 " in status and r.children == []
    c.close()


def test_a_wrong_protocol_version_is_a_400(run):
    r = run()
    c = Conn(r.port)
    assert " 400 " in c.upgrade(f"{page.SOCKET_PATH}?t={r.token}", host=r.host, origin=r.origin, version="8")
    c.close()


def test_an_unknown_socket_path_is_a_404(run):
    r = run()
    c = Conn(r.port)
    assert " 404 " in c.upgrade(f"/elsewhere?t={r.token}", host=r.host, origin=r.origin)
    c.close()


# -- the socket: what flows


def test_the_first_message_sizes_the_program_and_keystrokes_and_output_flow(run):
    r = run()
    c = r.connect(132, 43)
    until(lambda: r.children)
    assert r.session.size == (132, 43)
    c.send(w.BINARY, b"echo hi\r")
    assert c.frame() == (w.BINARY, b"echo hi\r\n")
    assert r.children[0].typed == b"echo hi\r"
    c.close()


def test_a_resize_message_resizes_once_and_a_repeat_does_nothing(run):
    r = run()
    c = r.connect(90, 25)
    until(lambda: r.children)
    for cols, rows in ((100, 30), (100, 30), (120, 40)):
        c.send_json({"type": "resize", "cols": cols, "rows": rows})
    until(lambda: r.children[0].sizes == [(100, 30), (120, 40)])
    c.send_json({"type": "resize", "cols": 0, "rows": 30})
    c.send_json({"type": "resize", "cols": "wide", "rows": 30})
    c.send_json({"type": "nonsense"})
    c.send(w.TEXT, b"not json")
    c.send(w.BINARY, b"x")
    c.frame()  # the echo of the last byte proves the earlier bad messages were skipped, not fatal
    assert r.children[0].sizes == [(100, 30), (120, 40)]
    c.close()


def test_a_large_paste_arrives_whole(run):
    r = run()
    c = r.connect()
    until(lambda: r.children)
    text = ("x" * 99 + "\r") * 600
    c.send(w.BINARY, text.encode())
    until(lambda: r.children[0].typed == text.encode())
    c.close()


def test_the_first_message_must_be_a_size(run):
    r = run()
    c = r.open()
    c.send(w.BINARY, b"typing before a size")
    assert c.frame()[0] == w.CLOSE and r.children == []
    c.close()
    c = r.open()
    c.send_json({"type": "resize", "cols": 99999, "rows": 5})
    assert c.frame()[0] == w.CLOSE and r.children == []
    c.close()


def test_a_client_that_never_sends_a_size_is_dropped(run, monkeypatch):
    monkeypatch.setattr(app, "FIRST_MESSAGE_SECONDS", 0.3)
    r = run()
    c = r.open()
    assert c.frame() is None or c.frame() is None  # the server ended the connection
    assert r.children == []
    c.close()


def test_an_oversized_frame_is_refused_and_the_session_survives(run, monkeypatch):
    monkeypatch.setattr(app, "MAX_MESSAGE", 2048)
    r = run()
    c = r.connect()
    until(lambda: r.children)
    c.send(w.BINARY, b"x" * 4096)
    opcode, payload = c.frame()
    assert opcode == w.CLOSE and struct.unpack(">H", payload[:2])[0] == w.CLOSE_TOO_BIG
    assert r.children[0].typed == b"" and r.children[0].closed is False
    c.close()


def test_a_broken_frame_closes_the_socket_with_a_protocol_error(run):
    r = run()
    c = r.connect()
    until(lambda: r.children)
    c.sock.sendall(w.encode_frame(w.TEXT, b"unmasked", mask=False))
    opcode, payload = c.frame()
    assert opcode == w.CLOSE and struct.unpack(">H", payload[:2])[0] == w.CLOSE_PROTOCOL
    c.close()


def test_a_ping_is_answered(run):
    r = run()
    c = r.connect()
    until(lambda: r.children)
    c.send(w.PING, b"are you there")
    assert c.frame() == (w.PONG, b"are you there")
    c.close()


# -- one client at a time


def test_a_second_window_takes_over_and_the_first_is_told(run):
    r = run()
    first = r.connect(80, 24)
    until(lambda: r.children)
    second = r.connect(120, 40)
    opcode, payload = first.frame()
    assert opcode == w.CLOSE and struct.unpack(">H", payload[:2])[0] == 4000
    until(lambda: r.session.size == (120, 40))
    first.close()
    second.send(w.BINARY, b"from the new window")
    until(lambda: r.children[0].typed == b"from the new window")
    assert len(r.children) == 1, "the program carries on; the window changes"
    second.close()


def test_the_window_that_was_replaced_cant_resize_or_type(run):
    r = run()
    first = r.connect(80, 24)
    until(lambda: r.children)
    second = r.connect(100, 30)
    until(lambda: r.session.size == (100, 30))
    first.sock.sendall(w.encode_frame(w.BINARY, b"ghost", mask=True))  # may be refused by a closed socket: fine
    time.sleep(0.2)
    assert r.children[0].typed == b""
    second.close()


def test_a_window_that_reconnects_gets_a_redraw_signal_at_the_same_size(run):
    r = run()
    c = r.connect(90, 25)
    until(lambda: r.children)
    c.close()
    until(lambda: r.session.client is None)
    c = r.connect(90, 25)
    until(lambda: r.children[0].winches == 1)
    c.close()


def test_when_the_program_exits_the_window_is_told(run):
    r = run()
    c = r.connect()
    until(lambda: r.children)
    r.children[0].emit(b"bye")
    r.children[0].finish(4)
    assert c.frame() == (w.BINARY, b"bye")
    opcode, payload = c.frame()
    assert opcode == w.TEXT and json.loads(payload) == {"type": "exit", "code": 4}
    c.close()


def test_a_program_that_cant_start_is_reported_to_the_page(run):
    def broken(cols, rows):
        raise OSError("no such program")

    session = bridge.Session(broken)
    server = app.build_server(session)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    try:
        c = Conn(server.port)
        c.upgrade(
            f"{page.SOCKET_PATH}?t={server.token}",
            host=f"127.0.0.1:{server.port}",
            origin=f"http://127.0.0.1:{server.port}",
        )
        c.send_json({"type": "resize", "cols": 80, "rows": 24})
        opcode, payload = c.frame()
        assert (
            opcode == w.TEXT
            and json.loads(payload)["type"] == "error"
            and "no such program" in json.loads(payload)["message"]
        )
        c.close()
    finally:
        server.stop()
        t.join(5)


# -- staying up while someone is attached


def test_the_server_stays_up_while_a_window_is_attached_and_stops_when_idle_without_one(run):
    r = run(idle_seconds=0.5)
    c = r.connect()
    until(lambda: r.children)
    time.sleep(1.2)
    assert r.thread.is_alive(), "an attached window keeps the server up past its idle time"
    c.close()
    until(lambda: not r.thread.is_alive(), seconds=4)


# -- a named address and a proxy's host names


def test_a_proxy_host_name_is_accepted_only_when_the_person_names_it(run):
    r = run(allow_hosts=["term.example:8443"])
    resp, _ = r.get("/", host="term.example:8443")
    assert resp.status == 200
    assert r.get("/", host="other.example:8443")[0].status == 403
    c = Conn(r.port)
    assert c.upgrade(
        f"{page.SOCKET_PATH}?t={r.token}", host="term.example:8443", origin="https://term.example:8443"
    ).startswith("HTTP/1.1 101")
    c.close()
    c = Conn(r.port)
    assert " 403 " in c.upgrade(
        f"{page.SOCKET_PATH}?t={r.token}", host="term.example:8443", origin="https://other.example:8443"
    )
    c.close()


def test_a_named_non_loopback_address_answers_only_to_itself():
    session = bridge.Session(lambda c, r: FakeChild())
    try:
        server = app.build_server(session, bind="127.0.0.2")
    except OSError:
        pytest.skip("this system doesn't give 127.0.0.2 to the loopback")
    try:
        assert server.httpd.socket.getsockname()[0] == "127.0.0.2"
        assert server.url.startswith("http://127.0.0.2:")
        assert f"127.0.0.2:{server.port}" in server.hosts
        assert f"127.0.0.1:{server.port}" not in server.hosts and f"localhost:{server.port}" not in server.hosts
        assert server.origins == {f"http://127.0.0.2:{server.port}"}
    finally:
        server.httpd.server_close()


def test_a_dropped_ui_server_default_is_unchanged_for_the_other_pages():
    server = ui_server.LoopbackServer({"/": lambda nonce: "x"})
    try:
        assert server.bind == "127.0.0.1" and server.hosts == {f"127.0.0.1:{server.port}", f"localhost:{server.port}"}
        assert server.httpd.socket.getsockname()[0] == "127.0.0.1" and server.upgrade_routes == {}
    finally:
        server.httpd.server_close()
