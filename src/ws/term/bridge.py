"""The pty bridge: one child process on a pseudo-terminal, and the one browser client that currently owns it.

`Session` holds the child (anything with the `Child` methods; `open_pty` makes the real one) and at most one attached
client. A client that attaches while another is attached takes over: the old one is closed with code 4000 and only the
new one can type or set the size, so two screens never fight over the grid. The size is the owner's alone: a resize from
anyone else is ignored, a repeat of the current size is ignored (the child isn't signalled for nothing), and a size out
of range is refused. The child starts on the first attach, at that client's size, so a full-screen app draws once at the
right size instead of redrawing after a resize. When a client detaches the child keeps running; the next client gets a
redraw signal. When the child exits, the client is told with its exit code, and `on_exit` runs.

Stdlib only; `fcntl` and `termios` are imported inside the functions that need them (not at import), so the module
loads on a machine without them.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

__all__ = [
    "CLOSE_REPLACED",
    "MAX_DIM",
    "Child",
    "Client",
    "PtyChild",
    "Session",
    "child_env",
    "open_pty",
    "valid_size",
]

CLOSE_REPLACED = 4000  # the WebSocket close code a client gets when another client takes the session over
MAX_DIM = 1000
# Run in the child before the program: make the pty its controlling terminal, then become the program.
_SHIM = "import fcntl,os,sys,termios\nfcntl.ioctl(0,termios.TIOCSCTTY,0)\nos.execvp(sys.argv[1],sys.argv[1:])"


def valid_size(cols: object, rows: object) -> bool:
    """A usable terminal size: whole numbers (a bool isn't one) from 1 to `MAX_DIM`."""
    return all(type(v) is int and 1 <= v <= MAX_DIM for v in (cols, rows))


def child_env(base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a session's program gets: the person's own, as a terminal that is not inside their tmux.

    `TERM` and `COLORTERM` describe the browser terminal. `TMUX` and `TMUX_PANE` go, because the session is not inside
    the tmux that launched `ws term` (and a program that sees them changes how it draws). `extra` is added last."""
    env = {k: v for k, v in base.items() if k not in ("TMUX", "TMUX_PANE")}
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.update(extra or {})
    return env


class Child(Protocol):
    """What a session needs from its program. `open_pty` makes the real one; tests pass a fake."""

    def read(self) -> bytes:
        """The next output, blocking; b"" once the program has gone."""

    def write(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def winch(self) -> None:
        """Ask the program to redraw (the size-change signal), without changing the size."""

    def wait(self) -> int:
        """The exit code, once the program has gone."""

    def close(self) -> None:
        """Hang up and stop the program."""


class Client(Protocol):
    def send_output(self, data: bytes) -> None: ...

    def send_event(self, event: dict[str, Any]) -> None: ...

    def close(self, code: int, reason: str) -> None: ...


class PtyChild:
    """A program on a real pseudo-terminal (POSIX)."""

    def __init__(self, proc: subprocess.Popen, fd: int):
        self.proc, self.fd = proc, fd
        self._closed = False

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 65536)
        except OSError:  # EIO on Linux: the other end of the pty has closed
            return b""

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view) :]

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def winch(self) -> None:
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGWINCH)

    def wait(self) -> int:
        return self.proc.wait()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            os.close(self.fd)
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGHUP)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


def open_pty(argv: Sequence[str], env: Mapping[str, str], cols: int, rows: int, cwd: str | None = None) -> PtyChild:
    """Start `argv` on a new pty of the given size, in its own session. Raises OSError if it can't start."""
    import fcntl
    import struct
    import termios

    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _SHIM, *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=dict(env),
            cwd=cwd,
            close_fds=True,
            start_new_session=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return PtyChild(proc, master)


class Session:
    """One child, and the one client that owns it. Every method is safe to call from any thread."""

    def __init__(
        self,
        spawn: Callable[[int, int], Child],
        *,
        on_exit: Callable[[int], None] | None = None,
        on_resize: Callable[[int, int], None] | None = None,
    ):
        self._spawn, self._on_exit, self._on_resize = spawn, on_exit, on_resize
        self._lock = threading.RLock()
        self._child: Child | None = None
        self._client: Client | None = None
        self._size: tuple[int, int] | None = None
        self.exit_code: int | None = None
        self._pump: threading.Thread | None = None

    @property
    def client(self) -> Client | None:
        return self._client

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    def attach(self, client: Client, cols: int, rows: int) -> bool:
        """Make `client` the owner at this size, closing the one it replaces. False if the size isn't usable."""
        if not valid_size(cols, rows):
            return False
        with self._lock:
            old, self._client = self._client, client
            if old is not None and old is not client:
                with contextlib.suppress(OSError):
                    old.close(CLOSE_REPLACED, "another window took this session over")
            if self._child is None and self.exit_code is None:
                self._child = self._spawn(cols, rows)
                self._size = (cols, rows)
                self._pump = threading.Thread(target=self._run, args=(self._child,), daemon=True)
                self._pump.start()
            elif self.exit_code is None:
                if not self._apply(cols, rows):
                    self._child.winch()  # same size: a signal is the only way to get the program to redraw
            else:
                self._tell(client, {"type": "exit", "code": self.exit_code})
        return True

    def detach(self, client: Client) -> None:
        with self._lock:
            if self._client is client:
                self._client = None

    def write(self, client: Client, data: bytes) -> bool:
        """Send the owner's keystrokes to the program. Anyone else's are dropped (False)."""
        with self._lock:
            if client is not self._client or self._child is None or self.exit_code is not None:
                return False
            child = self._child
        try:
            child.write(data)
        except OSError:
            return False
        return True

    def resize(self, client: Client, cols: int, rows: int) -> bool:
        """Set the size. Only the owner may; the same size again does nothing. True if the size changed."""
        if not valid_size(cols, rows):
            return False
        with self._lock:
            if client is not self._client or self._child is None or self.exit_code is not None:
                return False
            return self._apply(cols, rows)

    def _apply(self, cols: int, rows: int) -> bool:
        if self._size == (cols, rows) or self._child is None:
            return False
        self._size = (cols, rows)
        self._child.resize(cols, rows)
        if self._on_resize:
            self._on_resize(cols, rows)
        return True

    def close(self) -> None:
        with self._lock:
            child, client = self._child, self._client
            self._client = None
        if client is not None:
            with contextlib.suppress(OSError):
                client.close(1001, "ws term is closing")
        if child is not None:
            child.close()

    @staticmethod
    def _tell(client: Client, event: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            client.send_event(event)

    def _run(self, child: Child) -> None:
        """The output thread: pass what the program prints to whoever owns the session, until it exits."""
        while True:
            data = child.read()
            if not data:
                break
            client = self._client
            if client is not None:
                try:
                    client.send_output(data)
                except OSError:
                    self.detach(client)  # the browser has gone; the program keeps running
        code = child.wait()
        with self._lock:
            self.exit_code = code
            client = self._client
        if client is not None:
            self._tell(client, {"type": "exit", "code": code})
        if self._on_exit:
            self._on_exit(code)
