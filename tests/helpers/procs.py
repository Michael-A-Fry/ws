"""Process checks for tests that kill things: is a pid alive, and wait for it to be gone on the event, not a sleep.

A test that kills a process group and then checks a grandchild once races the kernel: SIGKILL is sent, but the process
still has to be scheduled to die, and on a loaded machine that takes a moment. `wait_gone` waits on the exit itself
(a pidfd becomes readable when the process has exited, Linux 5.3 and later) and falls back to polling /proc elsewhere.
A zombie counts as gone: it has exited and only waits for its parent to reap it.
"""

from __future__ import annotations

import os
import select
import time
from pathlib import Path


def alive(pid: int) -> bool:
    """True while `pid` is a running (not zombie) process."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return state not in ("Z", "X")


def wait_gone(pid: int, seconds: float = 10.0) -> bool:
    """Wait up to `seconds` for `pid` to exit; True once it has. Waits on a pidfd where there is one."""
    end = time.monotonic() + seconds
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is not None:
        try:
            fd = pidfd_open(pid)
        except ProcessLookupError:
            return True  # already exited and reaped
        except OSError:
            fd = None  # no pidfd on this kernel: poll instead
        if fd is not None:
            try:
                return bool(select.select([fd], [], [], max(0.0, end - time.monotonic()))[0]) or not alive(pid)
            finally:
                os.close(fd)
    while alive(pid):
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)
    return True
