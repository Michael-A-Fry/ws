"""`ws home` on loopback: the home page behind the shared token server, and the one call it may run.

It binds 127.0.0.1 on a random port and prints an address that carries the token (`ws.ui.server`). `GET /` builds a
fresh snapshot, fills each record's `opens` and returns the page; `POST /api/open` runs one opener's call for one
record, after the token, Host and Origin checks, and only a call the page itself offers for that record. Two more
calls run a program the profile names, behind the same checks: `POST /api/answer` answers an ask that `ws.home.api`
says can be answered (`[home.answer] command`), and `POST /api/start` starts a session from the box (`[home.start]
command`). Reading the page changes nothing.
"""

from __future__ import annotations

import html
import os
import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path

from ws.home import api, hub, page
from ws.home import open as opener
from ws.home.records import Snapshot
from ws.output import WsError
from ws.ui.server import BadRequest, LoopbackServer

__all__ = ["HomeServer", "build", "run"]

IDLE_SECONDS = 3600  # the page reloads itself while it is in view, so an hour of silence means nobody is looking


ANSWER_STATUS = {"not_found": 404, "refused": 409, "not_set": 409, "failed": 502, "timeout": 504}


def build(env: api.Env | None = None) -> tuple[Snapshot, api.Env]:
    """A fresh snapshot with its openers filled in, and the environment it was read in."""
    env = env or api.Env.make()
    return opener.attach(api.snapshot(env), env), env


def page_for(env: api.Env | None = None, nonce: str = "", *, live: bool = True) -> str:
    """The whole page from a fresh snapshot: what `GET /` serves and `ws home --render` writes."""
    snap, env = build(env)
    return page.render(snap, nonce, live=live, hub=hub.build(snap, env))


class HomeServer(LoopbackServer):
    """The home page over the shared loopback server. `env_factory` makes the environment for each request."""

    def __init__(self, env_factory=api.Env.make, *, token: str | None = None, idle_seconds: float = IDLE_SECONDS):
        self._env = env_factory
        self._sent: set[str] = set()  # asks answered or being answered: an ask is answered once
        self._sent_lock = threading.Lock()
        super().__init__(
            {"/": self._page, "/api/home": self._data},
            {"/api/open": self._open, "/api/answer": self._answer, "/api/start": self._start},
            token=token,
            idle_seconds=idle_seconds,
            server_version="ws-home",
            denied="This page needs the link that ws home printed. Open that link again.",
            failed="Something went wrong inside ws home. Run `ws doctor` to see where things stand.",
        )

    def _page(self, nonce: str) -> str:
        return page_for(self._env(), nonce)

    def _data(self, nonce: str) -> dict:
        snap, _ = build(self._env())
        return snap.to_dict()

    def _open(self, data: dict) -> tuple[int, dict]:
        want = [data.get(k) for k in ("opener", "kind", "id")]
        if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in want):
            raise BadRequest("opener, kind and id are text")
        return opener.perform(self._env(), *want)

    def _answer(self, data: dict) -> tuple[int, dict]:
        ident, choice, text = (data.get(k, "") for k in ("id", "choice", "text"))
        if not all(isinstance(v, str) for v in (ident, choice, text)) or not 0 < len(ident) <= 256:
            raise BadRequest("id, choice and text are text")
        if len(choice) > 256 or len(text) > api.ANSWER_TEXT_MAX:
            raise BadRequest("choice or text is too long")
        with self._sent_lock:
            if ident in self._sent:
                return 409, {"error": "that ask has already been answered", "hint": "check the session"}
            self._sent.add(ident)
        try:
            api.answer(ident, choice, text, self._env())
        except WsError as e:
            if e.kind not in ("failed", "timeout"):  # nothing ran, so the same ask can be tried again
                with self._sent_lock:
                    self._sent.discard(ident)
            body = {"error": e.message, "hint": e.hint}
            if e.kind in ("failed", "timeout"):
                body["may_have_sent"] = True  # the program ran, so it may have acted before it stopped
            return ANSWER_STATUS.get(e.kind, 502), body
        return 200, {"ok": True}

    def _start(self, data: dict) -> tuple[int, dict]:
        if not all(isinstance(data.get(k, ""), str) and len(data.get(k, "")) <= hub.REQUEST_MAX for k in data):
            raise BadRequest("every field is text")
        return hub.start(self._env(), data)


def _launcher(url: str) -> Path:
    """A local page that sends the browser to `url`, so the token is never on the browser's command line (where `ps`
    would show it). The folder is private to the person and goes when the server closes."""
    folder = Path(tempfile.mkdtemp(prefix="ws-home-"))  # created 0700
    path = folder / "open.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    safe = html.escape(url, quote=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={safe}">'
            f'<title>ws home</title><p>Opening ws home. If nothing happens, <a href="{safe}">open it</a>.</p>'
        )
    return path


def run(open_browser: bool = True) -> int:
    api.Env.make()  # a profile that can't be read stops here, with its own message, before anything binds
    server = HomeServer()
    print(f"ws home: {server.url}")
    print("Open it in a browser on this machine. It reloads itself, and closes after an hour with nobody looking.")
    launcher: Path | None = None
    try:
        if open_browser:
            launcher = _launcher(server.url)
            webbrowser.open(launcher.as_uri())
        server.serve()
    except KeyboardInterrupt:
        print("ws home: closed")
    finally:
        if launcher is not None:
            shutil.rmtree(launcher.parent, ignore_errors=True)
    return 0
