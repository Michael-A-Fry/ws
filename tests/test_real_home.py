"""A test can't reach the real home, even after `monkeypatch.undo()` (tests/conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import conftest


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
