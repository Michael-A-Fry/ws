"""Test support for the web terminal: a fake program that plays the part of the pty child.

`FakeChild` has the methods a `Session` needs (see `bridge.Child`). A test pushes output with `emit`, reads what the
browser typed from `written`, sees every size it was given in `sizes`, and ends it with `finish`. With `echo=True` it
prints back what it is sent, as a terminal in cooked mode does. `on_resize` lets a test play a full-screen program that
repaints when the size changes. Nothing here starts a process or touches a tty.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

__all__ = ["FakeChild"]


class FakeChild:
    def __init__(self, *, echo: bool = False, on_resize: Callable[[FakeChild, int, int], None] | None = None):
        self._out: queue.Queue[bytes] = queue.Queue()
        self._gone = threading.Event()
        self.echo, self._on_resize = echo, on_resize
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.winches = 0
        self.closed = False
        self.code = 0

    @property
    def typed(self) -> bytes:
        """Everything written to the program, as one byte string."""
        return b"".join(self.written)

    def emit(self, data: bytes) -> None:
        self._out.put(data)

    def finish(self, code: int = 0) -> None:
        """The program exits: `read` returns b"" once the queued output is drained."""
        self.code = code
        self._out.put(b"")

    # -- the Child methods
    def read(self) -> bytes:
        data = self._out.get()
        if data == b"":
            self._gone.set()
        return data

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if self.echo:
            self.emit(data.replace(b"\r", b"\r\n"))

    def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))
        if self._on_resize:
            self._on_resize(self, cols, rows)

    def winch(self) -> None:
        self.winches += 1

    def wait(self) -> int:
        self._gone.wait(5)
        return self.code

    def close(self) -> None:
        self.closed = True
        self.finish(0)
