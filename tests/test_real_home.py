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


def test_a_ws_folder_variable_from_the_outer_shell_does_not_reach_a_test(tmp_path):
    """Run a one-test suite with this conftest, from a shell that names ws's folders (as a ws session does)."""
    import subprocess
    import sys

    (tmp_path / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    (tmp_path / "test_env.py").write_text(
        "import os\n"
        "def test_none_of_them_is_set():\n"
        "    names = ['WS_CONFIG_DIR', 'WS_STATE_DIR', 'WS_DATA_DIR', 'WS_CACHE_DIR', 'XDG_STATE_HOME', 'XDG_DATA_HOME',\n"
        "             'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'WS_GATED_SESSION', 'WS_SANDBOXED', 'WS_APPROVAL_FD']\n"
        "    assert [n for n in names if n in os.environ] == []\n"
        "    assert os.environ['WS_HOME'].startswith(os.environ['HOME'])\n",
        encoding="utf-8",
    )
    outer = {
        **os.environ,
        "WS_STATE_DIR": str(tmp_path / "real-state"),
        "WS_CONFIG_DIR": str(tmp_path / "real-config"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "WS_GATED_SESSION": "1",
    }
    done = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        env=outer,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (tmp_path / "real-state").exists() and not (tmp_path / "xdg-state").exists()


def test_the_tripwire_watches_every_folder_ws_could_use_for_the_real_home(tmp_path):
    home = tmp_path / "realhome"
    outer = {
        "WS_STATE_DIR": str(tmp_path / "named-state"),
        "WS_CONFIG_DIR": str(tmp_path / "named-config"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
    }
    watched = {str(p) for p in conftest._ws_files_to_watch(outer, home)}
    for want in (
        home / ".ws" / "policy.toml",
        home / ".config" / "ws" / "policy.toml",
        home / ".local" / "state" / "ws" / "install-manifest.json",
        tmp_path / "xdg-config" / "ws" / "profile.toml",
        tmp_path / "named-state" / "install-manifest.json",
        tmp_path / "named-config" / "policy.toml",
    ):
        assert str(want) in watched, want
    only_home = {str(p) for p in conftest._ws_files_to_watch({"WS_HOME": str(tmp_path / "w")}, home)}
    assert str(tmp_path / "w" / "profile.toml") in only_home
