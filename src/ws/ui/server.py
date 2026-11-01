"""The loopback page server: one small server that any ws page (setup, home) is a table of routes on.

It binds 127.0.0.1 on a random port and makes a random token. The address carries the token; the page keeps it in
memory and sends it back in an `X-Ws-Token` header on every POST, so a page from another site cannot drive it. Every
request needs the right Host, a GET needs the token in its address, and a POST needs the token header and a matching
Origin. Each response carries a Content-Security-Policy: a page gets a per-response nonce for its one stylesheet and
one script (no `unsafe-inline`), and JSON or text is allowed nothing. A route that raises is a plain 500, never a
traceback. The server closes when a route stops it, on `close()`, or after `idle_seconds` with no request that
proved it had the token.

A GET route is `fn(nonce) -> str | dict`: a string is a page, a dict is JSON. A POST route is `fn(data) -> (status,
dict)` (or `(status, dict, True)` to stop the server once the reply is sent), called only after the token, Origin and
JSON checks pass; it raises `BadRequest` for input it can't use.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

__all__ = ["IDLE_SECONDS", "MAX_BODY", "BadRequest", "LoopbackServer", "csp"]

IDLE_SECONDS = 600
MAX_BODY = 64 * 1024

GetRoute = Callable[[str], "str | dict"]
PostRoute = Callable[[dict], "tuple[int, dict] | tuple[int, dict, bool]"]


class BadRequest(ValueError):
    """Raised by a POST route for a body it can't use; the person gets a 400."""


def csp(nonce: str | None = None) -> str:
    """The Content-Security-Policy for a response. The page gets a per-response nonce for its one script and one
    stylesheet (no `unsafe-inline`); everything else (JSON, text) is allowed nothing."""
    if nonce is None:
        return "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    return (
        f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


class LoopbackServer:
    """A local server over a table of routes. `serve()` runs until it is stopped or idle, and returns `result`."""

    result: Any = None

    def __init__(
        self,
        get: Mapping[str, GetRoute],
        post: Mapping[str, PostRoute] | None = None,
        *,
        token: str | None = None,
        idle_seconds: float = IDLE_SECONDS,
        server_version: str = "ws-ui",
        denied: str = "This page needs the link ws printed. Open that link again.",
        failed: str = "Something went wrong inside ws. Run `ws doctor` to see where things stand.",
    ):
        self.token = token or secrets.token_urlsafe(16)
        self.idle_seconds = idle_seconds
        self.get_routes, self.post_routes = dict(get), dict(post or {})
        self._denied, self._failed = denied.encode(), failed
        self._last = time.monotonic()
        self._done = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet: the terminal belongs to the person
                pass

            # -- checks
            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}")

            def _origin_ok(self) -> bool:
                origin = (self.headers.get("Origin") or "").lower()
                return origin in (f"http://127.0.0.1:{owner.port}", f"http://localhost:{owner.port}")

            def _send(self, code: int, body: bytes, kind: str, nonce: str | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", csp(nonce))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, doc: dict) -> None:
                self._send(code, json.dumps(doc).encode(), "application/json")

            def _same(self, a: str, b: str) -> bool:
                """Constant-time compare on bytes: a header with a non-ASCII character is a no, never a traceback."""
                try:
                    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
                except (UnicodeError, TypeError):
                    return False

            def _fail_500(self) -> None:
                try:
                    self._json(500, {"error": owner._failed})
                except OSError:  # the connection is already gone: nothing more to do, and no traceback on the terminal
                    return

            # -- routes
            def do_GET(self):
                try:
                    self._get()
                except Exception:  # noqa: BLE001 - a handler never raises: the person gets a plain 500, not a traceback
                    self._fail_500()

            def do_POST(self):
                try:
                    self._post()
                except Exception:  # noqa: BLE001 - a handler never raises: the person gets a plain 500, not a traceback
                    self._fail_500()

            def _get(self):
                q = parse_qs(urlsplit(self.path).query)
                if not self._host_ok() or not self._same((q.get("t") or [""])[0], owner.token):
                    return self._send(403, owner._denied, "text/plain")
                owner._last = time.monotonic()  # only a request that proves it has the token keeps the page alive
                route = owner.get_routes.get(urlsplit(self.path).path)
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                nonce = secrets.token_urlsafe(16)  # new for every response
                out = route(nonce)
                if isinstance(out, dict):
                    return self._json(200, out)
                self._send(200, out.encode(), "text/html", nonce)

            def _post(self):
                header = self.headers.get("X-Ws-Token") or ""
                if not (self._host_ok() and self._origin_ok() and self._same(header, owner.token)):
                    return self._send(403, b"forbidden", "text/plain")
                owner._last = time.monotonic()
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._send(415, b"send JSON", "text/plain")
                route = owner.post_routes.get(urlsplit(self.path).path)
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= n <= MAX_BODY:
                        raise ValueError
                    data = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(data, dict):
                        raise TypeError
                except (ValueError, TypeError):
                    return self._send(400, b"bad request", "text/plain")
                try:
                    out = route(data)
                except BadRequest:
                    return self._send(400, b"bad request", "text/plain")
                self._json(out[0], out[1])
                if len(out) > 2 and out[2]:  # the route ended the run: stop once its reply is sent
                    owner._done.set()

        Handler.server_version = server_version
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def stop(self) -> None:
        """Stop serving once the current request is answered."""
        self._done.set()

    def serve(self) -> Any:
        self.httpd.timeout = 0.2
        try:
            while not self._done.is_set():
                self.httpd.handle_request()
                if time.monotonic() - self._last > self.idle_seconds:
                    break
            if self._done.is_set():
                time.sleep(0.4)  # let the reply reach the page before the socket goes
        finally:
            self.httpd.server_close()
        return self.result

    def close(self) -> None:
        self._done.set()
