"""Two or more real processes race to take over a dead holder's lock: exactly one wins, with one died record."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

HOLDER = textwrap.dedent(
    """
    import sys, time
    from ws import joblock
    joblock.acquire("j", {"WS_HOME": sys.argv[1], "HOME": sys.argv[1]})
    print("held", flush=True)
    time.sleep(60)
    """
)
RACER = textwrap.dedent(
    """
    import os, sys, time
    from ws import joblock
    env = {"WS_HOME": sys.argv[1], "HOME": sys.argv[1]}
    while time.time() < float(sys.argv[2]):
        pass
    died = []
    try:
        lock = joblock.acquire("j", env, on_died=lambda n, o: died.append(1))
    except joblock.Held:
        print("held", len(died))
        sys.exit(0)
    print("got", len(died), flush=True)
    time.sleep(1.5)
    lock.release()
    """
)


def _py(code: str, *args: str, **kw):
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.Popen([sys.executable, "-c", code, *args], env=env, text=True, stdout=subprocess.PIPE, **kw)


@pytest.mark.parametrize("round_", range(4))
def test_racing_takeovers_of_a_killed_holder_give_one_winner_and_one_died_record(tmp_path, round_):
    home = str(tmp_path)
    holder = _py(HOLDER, home)
    assert holder.stdout.readline().strip() == "held"
    holder.send_signal(signal.SIGKILL)  # the holder dies with the lock taken
    holder.wait()
    lock = next((tmp_path / "locks").glob("j.lock"))
    old = time.time() - 3600
    os.utime(lock, (old, old))  # a lock file that looks old to a clock-based reader as well
    start = time.time() + 1.5
    racers = [_py(RACER, home, str(start)) for _ in range(6)]
    out = [p.communicate(timeout=60)[0].split() for p in racers]
    got = [o for o in out if o[0] == "got"]
    assert len(got) == 1, out
    assert sum(int(o[1]) for o in out) == 1, out  # one died record in all


def test_a_live_holder_is_never_taken_over_however_old_its_lock_file_looks(tmp_path):
    """A suspended or stalled run has a stale clock but still holds the lock: only its death may release it."""
    home = str(tmp_path)
    holder = _py(HOLDER, home)
    try:
        assert holder.stdout.readline().strip() == "held"
        lock = next((tmp_path / "locks").glob("j.lock"))
        old = time.time() - 3600
        os.utime(lock, (old, old))
        racer = _py(RACER, home, str(time.time()))
        assert racer.communicate(timeout=30)[0].split()[0] == "held"
    finally:
        holder.kill()
        holder.wait()
