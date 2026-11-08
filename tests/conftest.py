"""Fixtures every test gets.

A test must never write into the machine's real config. The runners find their user config through these variables
(`ws install` merges into Claude Code's `settings.json` and OpenCode's `opencode.json`), so they are taken out of every
test's environment; a test that needs one sets it to a folder under `tmp_path`.

A test run starts from a project `.venv`, which `ws install` refuses as the hook's interpreter (a folder that comes and
goes). The refusal has its own tests, which put the real check back.

Every test also starts with `HOME` and `WS_HOME` in a folder of its own, set by a MonkeyPatch the test can't undo, so
a test whose own `monkeypatch.undo()` drops its fixture's HOME still lands in a scratch home, not the person's. A
tripwire then fails any test after which files in the real home changed: ws's own (install manifest, profile, policy),
the runners' config (Claude Code's settings.json and skills, OpenCode's config), git and shell startup files, and the
entries of ~/.ssh (names, sizes and change times only; their contents are never read).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from ws import approvals, paths, userconfig

# The runners' config variables, the four XDG ones, ws's own folder variables (`WS_HOME` is set per test below) and the
# marks of a gated session: a test run inside a ws session would otherwise write to the folders that session names, or
# be refused as a session.
_CONFIG_VARS = (
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "OPENCODE_CONFIG",
    *paths.ENV_DIRS.values(),
    approvals.ENV_MARKER,
    approvals.SANDBOX_MARKER,
    approvals.FD_ENV,
)


_REAL_HOME = Path(os.path.expanduser("~"))
_OUTER_ENV = dict(os.environ)  # as the shell that started the run had it, before any test changes it
_WS_FILES = ("install-manifest.json", "profile.toml", "policy.toml")


def _real_ws_layouts(outer: dict[str, str], home: Path) -> list[paths.Layout]:
    """Every layout ws could be using for the real `home`: the one the starting shell's variables name, the XDG folders
    (with and without its XDG variables) and `~/.ws`."""
    plain = {"HOME": str(home)}
    out = [paths.xdg_layout(plain), paths.xdg_layout({**outer, **plain}), paths.Layout("legacy", *[home / ".ws"] * 4)]
    for env in ({**outer, "HOME": str(home)}, plain):
        with contextlib.suppress(Exception):  # an unclear layout is still covered by the others
            out.append(paths.layout(env))
    return out


def _ws_files_to_watch(outer: dict[str, str], home: Path) -> tuple[Path, ...]:
    found = [lay.path_of(name) for lay in _real_ws_layouts(outer, home) for name in _WS_FILES]
    return tuple(dict.fromkeys(found))


_WATCHED = (
    *_ws_files_to_watch(_OUTER_ENV, _REAL_HOME),
    _REAL_HOME / ".claude" / "settings.json",
    _REAL_HOME / ".claude" / "skills",
    _REAL_HOME / ".config" / "opencode" / "opencode.json",
    _REAL_HOME / ".gitconfig",
    _REAL_HOME / ".config" / "git" / "config",
    _REAL_HOME / ".bashrc",
    _REAL_HOME / ".bash_profile",
    _REAL_HOME / ".profile",
    _REAL_HOME / ".zshrc",
)
# Folders whose entries are watched one level deep: each entry's name, size and change time, never its contents.
_WATCHED_DIRS = (_REAL_HOME / ".ssh",)
# Entries other programs on the machine change while tests run (ssh adds hosts, agents make sockets).
_DIR_SKIP = ("known_hosts", "agent", "control", "cm-", "S.")


def _real_home_stamp() -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for p in _WATCHED:
        try:
            st = os.lstat(p)
        except OSError:
            out[str(p)] = None
        else:
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    for d in _WATCHED_DIRS:
        try:
            entries = [e for e in os.scandir(d) if not e.name.startswith(_DIR_SKIP)]
        except OSError:
            out[str(d)] = None
            continue
        out[str(d)] = (len(entries), 0)
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                out[e.path] = None
            else:
                out[e.path] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path_factory):
    """HOME and WS_HOME in a fresh folder, on a MonkeyPatch of its own; the tripwire around it."""
    before = _real_home_stamp()
    home = tmp_path_factory.mktemp("home")
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(home))
    mp.setenv("WS_HOME", str(home / ".ws"))
    yield
    mp.undo()
    after = _real_home_stamp()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, f"this test changed files in the real home: {changed}"


@pytest.fixture(autouse=True)
def _no_real_config():
    """Its own MonkeyPatch, not the test's: a test that calls `monkeypatch.undo()` (test_modules does) would put the
    shell's variables back and let `ws install` write skills into the real config."""
    mp = pytest.MonkeyPatch()
    for name in _CONFIG_VARS:
        mp.delenv(name, raising=False)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_real_managed_settings(tmp_path_factory):
    """The machine's own managed-settings files are not read: they could widen the wall and turn a test red."""
    from ws import wallsettings

    empty = tmp_path_factory.mktemp("managed")
    mp = pytest.MonkeyPatch()
    mp.setattr(wallsettings, "MANAGED_FILES", (str(empty / "managed-settings.json"),))
    mp.setattr(wallsettings, "MANAGED_DIRS", (str(empty / "managed-settings.d"),))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_resident_gate():
    """A hook command a test runs decides in its own process: no test starts a resident gate (`ws gate serve`) that would
    outlive it. The tests of the gate daemon (tests/test_gate_daemon.py) start their own in a folder of their own and stop
    it, so they take the variable back out."""
    mp = pytest.MonkeyPatch()
    mp.setenv("WS_GATE_DAEMON", "0")
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _interpreter_is_stable():
    mp = pytest.MonkeyPatch()
    mp.setattr(userconfig, "require_stable_interpreter", lambda cmd: None)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _signal_handlers_come_back():
    """A test that starts a launcher or a server in this process (they set handlers for Ctrl-C and `kill`) must not leave
    them behind: the next test that sends itself a real SIGINT would find the launcher's handler, not
    `KeyboardInterrupt`, and wait out its timeout. Each test's handlers are put back after it, whatever the order."""
    import signal

    names = ("SIGINT", "SIGTERM", "SIGHUP")
    before = {n: signal.getsignal(getattr(signal, n)) for n in names if hasattr(signal, n)}
    yield
    for name, handler in before.items():
        if handler is not None and signal.getsignal(getattr(signal, name)) != handler:
            signal.signal(getattr(signal, name), handler)
