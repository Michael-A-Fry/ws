"""Fixtures every test gets.

A test must never write into the machine's real config. The runners find their user config through these variables
(`ws install` merges into Claude Code's `settings.json` and OpenCode's `opencode.json`), so they are taken out of every
test's environment; a test that needs one sets it to a folder under `tmp_path`.

A test run starts from a project `.venv`, which `ws install` refuses as the hook's interpreter (a folder that comes and
goes). The refusal has its own tests, which put the real check back.

Every test also starts with `HOME` and `WS_HOME` in a folder of its own, set by a MonkeyPatch the test can't undo, so
a test whose own `monkeypatch.undo()` drops its fixture's HOME still lands in a scratch home, not the person's. A
tripwire then fails any test after which ws's own files in the real home changed (install manifest, profile, policy,
Claude Code's settings.json, the skills folder).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ws import userconfig

_CONFIG_VARS = (
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "OPENCODE_CONFIG",
)


_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_WS_HOME = Path(os.environ.get("WS_HOME") or _REAL_HOME / ".ws")
_WATCHED = (
    _REAL_WS_HOME / "install-manifest.json",
    _REAL_WS_HOME / "profile.toml",
    _REAL_WS_HOME / "policy.toml",
    _REAL_HOME / ".claude" / "settings.json",
    _REAL_HOME / ".claude" / "skills",
    _REAL_HOME / ".config" / "opencode" / "opencode.json",
)


def _real_home_stamp() -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for p in _WATCHED:
        try:
            st = os.lstat(p)
        except OSError:
            out[str(p)] = None
        else:
            out[str(p)] = (st.st_mtime_ns, st.st_size)
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
    changed = [k for k, v in _real_home_stamp().items() if v != before[k]]
    assert not changed, f"this test changed the real home's ws files: {changed}"


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
def _interpreter_is_stable():
    mp = pytest.MonkeyPatch()
    mp.setattr(userconfig, "require_stable_interpreter", lambda cmd: None)
    yield
    mp.undo()
