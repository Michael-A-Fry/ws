"""The one lock between the upgrade service and a hand-run upgrade (`upgrade.upgrade_lock`): a second
holder waits a bounded time or refuses, a holder that dies drops it, and the hooks step of a swap is not locked out of its
own parent's lock. The service's side of it is in tests/test_upgrade_unattended.py."""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ws import approvals, cli, paths, upgrade
from ws.term import bridge


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setattr(paths, "account_home", lambda: home.resolve())
    monkeypatch.setenv("WS_GATE_DAEMON", "0")
    for var in (approvals.ENV_MARKER, approvals.SANDBOX_MARKER, approvals.FD_ENV, *bridge.RUNNER_SESSION_NAMES):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(home)
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(upgrade, "LOCK_WAIT_HAND_S", 0.3)  # a hand-run upgrade gives up quickly in a test
    return home


def lock_path() -> Path:
    return paths.state_dir(dict(os.environ)) / upgrade.LOCK_FILE


@contextlib.contextmanager
def held():
    """Another holder of the lock: its own open file, so `flock` sees a second owner even in this process."""
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


def free() -> bool:
    try:
        with upgrade.upgrade_lock(os.environ, 0):
            return True
    except upgrade.Locked:
        return False


def test_a_second_holder_waits_its_time_and_then_refuses_it_never_waits_for_ever(box):
    with held():
        t = time.monotonic()
        with pytest.raises(upgrade.Locked) as seen, upgrade.upgrade_lock(os.environ, 0.3):
            pytest.fail("took a lock that is held")
        assert 0.25 <= time.monotonic() - t < 5
        assert "holds the upgrade lock" in seen.value.message and seen.value.exit_code == upgrade.EXIT_REFUSED
    assert free()


def test_a_second_holder_gets_it_as_soon_as_the_first_lets_go(box):
    with upgrade.upgrade_lock(os.environ, 0):
        assert not free()  # the lock is not re-entrant: a nested take is a second holder
    assert free() and free()  # let go on the way out, so the next one is not kept out


def test_the_lock_is_dropped_when_its_holder_dies(box, tmp_path):
    """The kernel drops an `flock` with the process, so a crash or a kill mid-swap never leaves the next run locked out."""
    script = tmp_path / "holder.py"
    script.write_text(
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, str(script), str(lock_path())], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert not free()
    finally:
        proc.kill()
        proc.wait()
    assert free()


def test_a_hand_run_upgrade_while_another_runs_refuses_and_changes_nothing(box, capsys):
    with held():
        for argv in (["upgrade", "--hooks-only"], ["upgrade", "--rollback"], ["upgrade"]):
            t = time.monotonic()
            assert cli.main(argv) == upgrade.EXIT_REFUSED, argv
            assert time.monotonic() - t < 5
            assert "holds the upgrade lock" in capsys.readouterr().err
        assert not (box / ".claude").exists()  # nothing was written
    assert free()


def test_a_dry_run_and_print_units_do_not_need_the_lock(box, capsys):
    with held():
        assert cli.main(["upgrade", "--dry-run"]) != upgrade.EXIT_REFUSED
        assert "upgrade lock" not in capsys.readouterr().err


def test_the_hooks_step_of_a_swap_runs_under_its_parents_lock(box, monkeypatch, capsys):
    """The parent holds the lock while it runs `ws upgrade --hooks-only` as a step: that child must not wait for it."""
    monkeypatch.setenv(upgrade.STEP_ENV, "1")
    with held():
        cli.main(["upgrade", "--hooks-only"])
        assert "upgrade lock" not in capsys.readouterr().err
