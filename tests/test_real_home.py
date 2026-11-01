"""A test can't reach the real home, even after `monkeypatch.undo()` (tests/conftest.py)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

# by path: `import conftest` can pick up tests/roles/conftest.py instead
_spec = importlib.util.spec_from_file_location("ws_tests_conftest", Path(__file__).with_name("conftest.py"))
assert _spec and _spec.loader
conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conftest)


def test_home_is_a_scratch_folder_even_after_undo(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "mine"))
    monkeypatch.undo()
    home = Path(os.environ["HOME"])
    assert home != conftest._REAL_HOME
    assert Path(os.environ["WS_HOME"]) == home / ".ws"


def test_the_tripwire_sees_a_change_to_a_watched_file(tmp_path, monkeypatch):
    watched = tmp_path / "install-manifest.json"
    monkeypatch.setattr(conftest, "_WATCHED", (watched,))
    before = conftest._real_home_stamp()
    watched.write_text("{}")
    assert conftest._real_home_stamp() != before
