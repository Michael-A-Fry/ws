"""The reveal-once page: a value ws generated, shown to its owner one time, on their own device, then never again.

`ws secret reveal NAME` serves one small page at one tailnet address (`ws.tailnet.bind_ok`), on a random port, at a
random 32-byte path. Only the owner's own untagged device gets it (`ws.tailnet.whois_ok`); everyone else, and every
other path, gets a plain 404. The rules, each pinned by a test:

- a GET shows a "Show" button and never the value, so a link preview or a prefetch cannot burn it;
- the value is shown only in the answer to that button's POST, and only to a same-origin POST;
- before the answer is sent, a marker (no value in it) is written to `revealed/` in the state folder and the page is
  marked used, so a second POST, a retry or a later `ws secret reveal` gets nothing (`--again` clears the marker);
- every response carries `Cache-Control: no-store`, no referrer, no framing and a CSP that allows no script at all;
- the server logs nothing, so the token never reaches a log;
- the page expires after 15 minutes, and the server exits after one use.

The value is read from the store only for that one answer and dropped straight after.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import os
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ws import paths, tailnet
from ws.secrets import NAME_RE, SecretsError, Store

LIFETIME_S = 15 * 60
MAX_BODY = 1024


class RevealError(Exception):
    """The page cannot start. The message is safe to print (never a value or the token)."""


def marker_dir(env: dict | None = None) -> Path:
    """Where the "already shown" markers live: `revealed/` in the state folder. A marker holds a time, never a value."""
    return paths.state_dir(env) / "revealed"


def consumed(name: str, folder: Path) -> bool:
    return (folder / name).exists()


def clear(name: str, folder: Path) -> None:
    """Forget that `name` was shown (a new value was generated, or the person asked again)."""
    with contextlib.suppress(FileNotFoundError):
        (folder / name).unlink()


def _mark(name: str, folder: Path) -> bool:
    """Write the marker; False if it was already there (someone else got the value first)."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>ws: one-time value</title>
<style nonce="{nonce}">
:root{{color-scheme:light dark;--bg:#fafaf7;--fg:#1b1d21;--mute:#5d6470;--box:#eef0ec;--edge:#c9cdc4;--act:#1f5fbf}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--fg:#e6e9ee;--mute:#9aa4b1;--box:#1b2027;--edge:#39424e;--act:#7fb0ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:17px/1.45 system-ui,sans-serif}}
main{{max-width:32rem;margin:0 auto;padding:24px 16px}}h1{{font-size:21px;margin:0 0 8px}}p{{color:var(--mute);margin:0 0 14px}}
button{{width:100%;padding:15px;border:0;border-radius:10px;background:var(--act);color:var(--bg);font-size:17px;font-weight:600}}
code{{display:block;padding:14px;border-radius:10px;background:var(--box);border:1px solid var(--edge);
font:16px/1.4 ui-monospace,monospace;word-break:break-all;user-select:all;margin:0 0 14px}}
</style></head><body><main>{body}</main></body></html>"""


def _csp(nonce: str) -> str:
    return f"default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


def _send(handler: BaseHTTPRequestHandler, code: int, body: str) -> None:
    """One whole response with the page headers. Separate so a test can watch what goes out and when."""
    nonce = secrets.token_urlsafe(12)
    data = _PAGE.format(nonce=nonce, body=body).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for k, v in tailnet.headers(_csp(nonce)):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


class Page:
    """One reveal page. `serve(lifetime)` blocks until it is used or expires, then shuts the server down and returns
    the result: "revealed", "expired", "stopped", "failed" (the store could not be read; nothing was shown) or "taken"
    (another page showed it first)."""

    def __init__(self, name: str, store: Store, folder: Path, allowed: Callable[[str], bool]):
        self.name = name
        self.store = store
        self.folder = folder
        self.allowed = allowed
        self.token = secrets.token_urlsafe(32)
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.used = False
        self.result = "waiting"
        self.server: ThreadingHTTPServer  # set by make_page

    def path_ok(self, path: str) -> bool:
        return hmac.compare_digest(path.encode("utf-8", "replace"), f"/{self.token}".encode())

    def serve(self, lifetime: float = LIFETIME_S) -> str:
        t = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t.start()
        if not self.done.wait(lifetime):
            with self.lock:
                if not self.used:
                    self.used = True  # nothing more is answered with the value, even mid-shutdown
                    self.result = "expired"
        self.server.shutdown()
        self.server.server_close()
        t.join(5)
        return self.result

    def stop(self) -> None:
        with self.lock:
            if not self.used:
                self.used = True
                self.result = "stopped"
        self.done.set()

    def reveal(self, handler: BaseHTTPRequestHandler) -> None:
        """Answer the Show button: mark used (in memory and on disk), then send the value, once."""
        gone = "<h1>Already used</h1><p>This page has done its job. Nothing more here.</p>"
        with self.lock:
            if self.used:
                return _send(handler, 410, gone)
            self.used = True
            if not _mark(self.name, self.folder):  # another page or process showed it first: this one is finished
                self.result = "taken"
                try:
                    return _send(handler, 410, gone)
                finally:
                    self.done.set()
            try:
                value: str | None = self.store.read(self.name)
            except (FileNotFoundError, SecretsError):
                clear(self.name, self.folder)  # nothing left the machine, so a new link may show it
                self.result = "failed"
                try:
                    return _send(handler, 410, "<h1>Nothing to show</h1><p>ws could not read the value.</p>")
                finally:
                    self.done.set()
            self.result = "revealed"
        body = (
            f"<h1>{html.escape(self.name)}</h1>"
            "<p>Save this in your password manager now. ws will not show it again.</p>"
            f"<code>{html.escape(value or '')}</code><p>You can close this page.</p>"
        )
        value = None
        try:
            _send(handler, 200, body)
        finally:
            body = ""
            self.done.set()


def make_page(
    name: str,
    addr: str,
    store: Store,
    allowed: Callable[[str], bool],
    allow_loopback: bool = False,
    markers: Path | None = None,
    again: bool = False,
    port: int = 0,
) -> Page:
    """A reveal page for `name`, listening (not yet serving) on `addr`. Raises RevealError when `addr` is not one
    tailnet address (loopback too with allow_loopback), when the name has no value, or when it was already shown
    (unless `again`)."""
    if not NAME_RE.match(name):
        raise RevealError("not a valid secret name")
    if not tailnet.bind_ok(addr, allow_loopback=allow_loopback):
        raise RevealError(f"{addr!r} is not a single tailnet address; the page listens on nothing else")
    folder = markers if markers is not None else marker_dir()
    if consumed(name, folder):
        if not again:
            raise RevealError(f"{name} was already shown once; `--again` shows it one more time")
        clear(name, folder)
    try:
        if name not in store.names():
            raise FileNotFoundError(name)
        store.read(name)
    except PermissionError:
        raise RevealError("the store belongs to another user, so this user cannot show its values") from None
    except FileNotFoundError:
        raise RevealError(f"{name} has no value to show; `ws secret generate {name}` makes one") from None
    except SecretsError as e:
        raise RevealError(f"{name}: {e}") from None
    page = Page(name, store, folder, allowed)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ws"
        sys_version = ""

        def log_message(self, format, *args):
            pass

        def log_error(self, format, *args):
            pass

        def _gate(self) -> bool:
            if not page.path_ok(self.path) or not page.allowed(self.client_address[0]):
                _send(self, 404, "<h1>Not found</h1>")
                return False
            return True

        def do_GET(self):
            if not self._gate():
                return
            if page.used:
                return _send(self, 410, "<h1>Already used</h1><p>This page has done its job. Nothing more here.</p>")
            _send(
                self,
                200,
                f"<h1>{html.escape(page.name)}</h1><p>ws made this value for you. It shows once: have your password "
                "manager ready, then tap Show.</p>"
                f'<form method="post" action="/{page.token}"><button type="submit">Show</button></form>',
            )

        def do_POST(self):
            if not self._gate():
                return
            n = self.headers.get("Content-Length") or "0"
            with contextlib.suppress(ValueError, OSError):
                self.rfile.read(max(0, min(int(n), MAX_BODY)))
            origin = self.headers.get("Origin")
            site = self.headers.get("Sec-Fetch-Site")
            host = self.headers.get("Host") or ""
            if (origin and origin not in (f"http://{host}", "null")) or site in ("cross-site", "same-site"):
                return _send(self, 403, "<h1>Refused</h1><p>Open the link itself, not from another page.</p>")
            page.reveal(self)

        def do_HEAD(self):
            self.send_response(405)
            self.end_headers()

        do_PUT = do_DELETE = do_PATCH = do_HEAD

    try:
        server = ThreadingHTTPServer((addr.strip(), port), Handler)
    except OSError as e:
        raise RevealError(f"could not listen on {addr}: {e.strerror or type(e).__name__}") from None
    server.daemon_threads = True
    page.server = server
    return page
