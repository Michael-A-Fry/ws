"""The web terminal as a server: the page, its one script, and the socket that joins a browser to a `Session`.

`build_server` puts three routes on the loopback server: the page, the vendored terminal script (by its hash), and the
WebSocket. The socket's first message must be the client's size (`{"type":"resize","cols":N,"rows":M}`), which starts the
program at that size. After that, binary frames are keystrokes and text frames are `resize` messages; the server sends
the program's output as binary frames and `{"type":"exit","code":N}` or `{"type":"error","message":...}` as text. A
second browser that connects takes the session over (the first is closed with 4000), so only one window has a size and
a keyboard at a time. The server never reads the process environment into a response: the page is built from fixed
data only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ws.term import assets, page
from ws.term.bridge import Session
from ws.term.websocket import BINARY, TEXT, HandshakeError, ProtocolError, WebSocket, check_handshake
from ws.ui.server import BadRequest, LoopbackServer, Raw, Upgrade, csp

__all__ = ["FIRST_MESSAGE_SECONDS", "WebClient", "build_server"]

FIRST_MESSAGE_SECONDS = 10.0
MAX_MESSAGE = 4 * 1024 * 1024  # the largest paste the page can send in one frame


class WebClient:
    """One browser on a WebSocket, as the session sees it."""

    def __init__(self, ws: WebSocket):
        self.ws = ws

    def send_output(self, data: bytes) -> None:
        self.ws.send_binary(data)

    def send_event(self, event: dict[str, Any]) -> None:
        self.ws.send_text(json.dumps(event))

    def close(self, code: int, reason: str) -> None:
        self.ws.close(code, reason)


def _resize_of(kind: int, data: bytes) -> tuple[int, int] | None:
    """The size in a text message, if it's a resize."""
    if kind != TEXT:
        return None
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "resize":
        return None
    cols, rows = msg.get("cols"), msg.get("rows")
    return (cols, rows) if type(cols) is int and type(rows) is int else None


def _serve_socket(session: Session, req: Upgrade) -> None:
    try:
        accept = check_handshake(req.headers)
    except HandshakeError as e:
        raise BadRequest(str(e)) from e
    req.accept(accept)
    ws = WebSocket(req.rfile, req.wfile, max_message=MAX_MESSAGE)
    client = WebClient(ws)
    try:
        req.connection.settimeout(FIRST_MESSAGE_SECONDS)
        first = ws.recv()
        size = _resize_of(*first) if first else None
        if size is None:
            ws.close(1002, "the first message must be the size")
            return
        req.connection.settimeout(None)
        try:
            ok = session.attach(client, *size)
        except Exception as e:  # noqa: BLE001 - the program couldn't start: tell the page, don't drop the socket
            client.send_event({"type": "error", "message": f"Couldn't start the session: {e}"})
            ws.close(1011, "start failed")
            return
        if not ok:
            ws.close(1008, "that size isn't usable")
            return
        while True:
            msg = ws.recv()
            if msg is None:
                return
            kind, data = msg
            if kind == BINARY:
                session.write(client, data)
            elif (size := _resize_of(kind, data)) is not None:
                session.resize(client, *size)
    except (ProtocolError, OSError, TimeoutError):
        pass
    finally:
        session.detach(client)
        ws.close()


def build_server(
    session: Session,
    *,
    bind: str = "127.0.0.1",
    port: int = 0,
    allow_hosts: Iterable[str] = (),
    idle_seconds: float = 3600.0,
    token: str | None = None,
) -> LoopbackServer:
    """The loopback server for one session. It stays up while a browser is attached, and stops after `idle_seconds`
    with none."""
    bundle = assets.bundle()
    integrity = page.script_integrity(bundle)
    xterm_css = assets.load("xterm.css").decode("utf-8")
    holder: dict[str, LoopbackServer] = {}

    def index(nonce: str) -> Raw:
        html = page.page_html(nonce, token=holder["server"].token, xterm_css=xterm_css, integrity=integrity)
        return Raw(html.encode("utf-8"), "text/html", page.policy(nonce, integrity))

    def script(_nonce: str) -> Raw:
        return Raw(bundle, "text/javascript", csp())

    server = LoopbackServer(
        {"/": index, page.ASSET_PATH: script},
        upgrade={page.SOCKET_PATH: lambda req: _serve_socket(session, req)},
        token=token,
        idle_seconds=idle_seconds,
        server_version="ws-term",
        denied="This page needs the link ws term printed. Open that link again.",
        bind=bind,
        port=port,
        allow_hosts=allow_hosts,
    )
    holder["server"] = server
    server.hold = lambda: session.client is not None
    return server
