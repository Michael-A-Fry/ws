"""Fixtures every test gets.

A test must never write into the machine's real config. The runners find their user config through these variables
(`ws install` merges into Claude Code's `settings.json` and OpenCode's `opencode.json`), so they are taken out of every
test's environment; a test that needs one sets it to a folder under `tmp_path`.

A test run starts from a project `.venv`, which `ws install` refuses as the hook's interpreter (a folder that comes and
goes). The refusal has its own tests, which put the real check back.
"""

from __future__ import annotations

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
def _own_home(tmp_path):
    """HOME and WS_HOME start under the test's `tmp_path`, so code that reads either (a policy reads the profile for
    the secrets folder) never sees the machine's own. A test that needs another sets it; its own monkeypatch wins."""
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(tmp_path / "autohome"))
    mp.setenv("WS_HOME", str(tmp_path / "autohome" / ".ws"))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _interpreter_is_stable():
    mp = pytest.MonkeyPatch()
    mp.setattr(userconfig, "require_stable_interpreter", lambda cmd: None)
    yield
    mp.undo()
