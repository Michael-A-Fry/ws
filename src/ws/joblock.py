"""A job's run lock: a file with a heartbeat and a fence number, not a pid.

A holder touches the lock file's mtime every `HEARTBEAT_S` while it works. A second runner of the same job finds the
lock fresh and skips (coalesce: a double tick runs the job once). A lock whose heartbeat is older than `STALE_S`
belongs to a holder that died (a pid can be reused, a heartbeat cannot be faked by a dead process): the next runner
takes it over and writes one `died` record for the run that never finished. Every holder gets a fence number, one
above the last; a holder whose number is no longer the file's has lost the lock and must stop writing.

The next run time is systemd's (or launchd's), advanced before the unit is dispatched, so ws keeps no next-run state of
its own; this lock is the one tick lock that makes two ticks one run.

Swap note: the lock is a plain file with `O_EXCL` and a rename; a different mutex (a database row with a lease) can
replace `acquire` and `Lock.release` without touching a caller.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ws import jobs, paths

HEARTBEAT_S = 15.0
STALE_S = 120.0  # eight missed heartbeats; a run that stops beating this long is dead


class Held(Exception):
    """The job's lock is held by a live holder."""


@dataclass
class Lock:
    path: Path
    fence: int
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _beat: threading.Thread | None = field(default=None, repr=False)
    lost: bool = False  # True once another holder has the file: stop writing

    def _owns(self) -> bool:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("fence") == self.fence
        except (OSError, ValueError):
            return False

    def beat(self) -> bool:
        """Touch the heartbeat. False (and `lost`) when the lock is no longer this holder's."""
        if not self._owns():
            self.lost = True
            return False
        with contextlib.suppress(OSError):
            os.utime(self.path)
        return True

    def start(self, interval: float = HEARTBEAT_S) -> None:
        def run() -> None:
            while not self._stop.wait(interval):
                if not self.beat():
                    return

        self._beat = threading.Thread(target=run, daemon=True, name="ws-job-heartbeat")
        self._beat.start()

    def release(self) -> None:
        self._stop.set()
        if self._beat:
            self._beat.join(timeout=2)
        if self._owns():  # never remove a lock another holder took over
            with contextlib.suppress(OSError):
                self.path.unlink()


def locks_dir(env: dict | None = None) -> Path:
    return paths.state_dir(env) / "locks"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _next_fence(fence_file: Path) -> int:
    n = (_read(fence_file) or {}).get("fence", 0)
    n = n + 1 if isinstance(n, int) and n >= 0 else 1
    tmp = fence_file.with_name(fence_file.name + f".{os.getpid()}")
    tmp.write_text(json.dumps({"fence": n}), encoding="utf-8")
    os.replace(tmp, fence_file)
    return n


def acquire(
    name: str,
    env: dict | None = None,
    now: Callable[[], float] = time.time,
    stale_s: float = STALE_S,
    on_died: Callable[[str, dict], None] | None = None,
) -> Lock:
    """Take the lock of job `name`. Raises Held when a live holder has it. A stale holder is taken over: its lock file is
    renamed away (only one racer wins the rename) and `on_died(name, old)` is called once with what the file said."""
    if not jobs.NAME.match(name):
        raise ValueError(f"{name!r} is not a job name")
    d = locks_dir(env)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{name}.lock"
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = now() - path.stat().st_mtime
            except OSError:
                continue  # released between the open and the stat: try again
            if age < stale_s:
                raise Held(f"job {name} is already running (fence {(_read(path) or {}).get('fence', '?')})") from None
            tomb = path.with_name(f"{name}.lock.died.{os.getpid()}")
            try:
                os.rename(path, tomb)
            except OSError:
                continue  # another runner took it over first
            old = _read(tomb) or {}
            with contextlib.suppress(OSError):
                tomb.unlink()
            if on_died:
                on_died(name, old)
            continue
        fence = _next_fence(d / f"{name}.fence")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fence": fence, "pid": os.getpid(), "started": now()}, f)
        return Lock(path, fence)
    raise Held(f"job {name}: could not take the lock (contended)")


@contextlib.contextmanager
def hold(name: str, env: dict | None = None, on_died: Callable[[str, dict], None] | None = None, **kw):
    """`with hold(name) as lock:` runs the body holding the lock with a heartbeat; `lock` is None when a live holder has
    it (the caller skips: one tick, one run)."""
    try:
        lock = acquire(name, env, on_died=on_died, **kw)
    except Held:
        yield None
        return
    lock.start()
    try:
        yield lock
    finally:
        lock.release()
