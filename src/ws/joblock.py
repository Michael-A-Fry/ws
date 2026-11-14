"""A job's run lock: an advisory `flock` on a file the job owns, and a fence number in the file.

The kernel holds the lock for as long as the holder's process lives and drops it the moment the process dies, however
it dies (a kill, an out-of-memory stop, a power cut). So there is no heartbeat to miss and no clock to trust: a run that
is suspended or slow still holds the lock, and a run that is gone never does. A second runner of the same job finds the
lock held and skips (coalesce: a double tick runs the job once).

The file stays in place. Each holder writes `{"fence", "pid", "running": true}` when it takes the lock and
`"running": false` when it lets go cleanly. A new holder that finds `"running": true` has taken over from a holder that
died: it calls `on_died` once (it holds the lock exclusively, so no other runner can see the same file in that state) and
takes the next fence number. A holder whose fence is no longer the file's has been taken over and must stop writing.

The next run time is systemd's (or launchd's), advanced before it starts the unit, so ws keeps none of its own; this
lock is the one tick lock that makes two ticks one run.

Platforms: `fcntl` is POSIX. Where it is missing the lock is not taken (the job still runs), so `ws doctor` still starts
on native Windows, which points at WSL2.

Swap note: a different mutex (a database row with a lease) can replace `acquire` and `Lock.release` without touching a
caller.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ws import jobs, paths


class Held(Exception):
    """The job's lock is held by a live holder."""


@dataclass
class Lock:
    path: Path
    fence: int
    fd: int | None = None

    def owns(self) -> bool:
        """True while the file still carries this holder's fence (False after a takeover)."""
        return _read(self.path).get("fence") == self.fence

    def release(self) -> None:
        """Mark a clean end, then let go of the lock. A holder that lost the lock writes nothing."""
        if self.fd is None:
            return
        try:
            if self.owns():
                _write(self.fd, {"fence": self.fence, "pid": os.getpid(), "running": False})
        finally:
            with contextlib.suppress(OSError):
                os.close(self.fd)  # closing the descriptor releases the flock
            self.fd = None


def locks_dir(env: dict | None = None) -> Path:
    return paths.state_dir(env) / "locks"


def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(fd: int, rec: dict) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(rec).encode())


def acquire(
    name: str,
    env: dict | None = None,
    on_died: Callable[[str, dict], None] | None = None,
) -> Lock:
    """Take the lock of job `name`. Raises Held when a live holder has it. When the file says the last holder was still
    running, that holder died: `on_died(name, what the file said)` is called once and the fence goes up by one."""
    if not jobs.NAME.match(name):
        raise ValueError(f"{name!r} is not a job name")
    d = locks_dir(env)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{name}.lock"
    try:
        import fcntl
    except ImportError:  # no flock on this platform: run without the lock
        return Lock(path, 0)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise Held(f"job {name} is already running (fence {_read(path).get('fence', '?')})") from None
    old = _read(path)
    fence = old.get("fence", 0)
    fence = fence + 1 if isinstance(fence, int) and fence >= 0 else 1
    _write(fd, {"fence": fence, "pid": os.getpid(), "running": True})
    if old.get("running") is True and on_died:
        with contextlib.suppress(Exception):  # a record that cannot be written must not stop the run
            on_died(name, old)
    return Lock(path, fence, fd)


@contextlib.contextmanager
def hold(name: str, env: dict | None = None, on_died: Callable[[str, dict], None] | None = None):
    """`with hold(name) as lock:` runs the body holding the lock; `lock` is None when a live holder has it (the caller
    skips: one tick, one run)."""
    try:
        lock = acquire(name, env, on_died=on_died)
    except Held:
        yield None
        return
    try:
        yield lock
    finally:
        lock.release()
