"""The protected paths of the unattended upgrade's units and the installed hook: a session can't write a unit, a drop-in or
a `user.control` copy of one, and can still write the request file. The hook probes run the installed PreToolUse command,
as a watchdog does. The guard's own words are in tests/test_guard_upgrade.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ws import cli, selfcmd, upgrade, upgradecheck, upgradeunit

# ---- the installed hook, probed the way a watchdog probes it -----------------------------------------------------------------


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tool = home / ".local" / "share" / "uv" / "tools" / "ws-aitoolkit"
    (tool / "bin").mkdir(parents=True)
    (tool / "bin" / "ws").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("WS_GATE_DAEMON", "0")
    monkeypatch.setattr(selfcmd, "tree", lambda: tool)
    assert cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(home / "Notes")]) == 0
    assert upgrade.migrate_policy(os.environ)  # the upgrade adds the guard to an existing policy, once
    return home, tool


def exit_code(command: str) -> int:
    hook = upgradecheck.hook_command(os.environ)
    assert hook is not None
    return upgradecheck.probe(hook, command, os.environ)


def test_the_live_hook_refuses_what_the_guard_and_the_protected_paths_say(box):
    home, tool = box
    units = home / ".config" / "systemd" / "user"
    refused = [
        "ws upgrade --unattended",
        "systemctl --user start ws-upgrade.service",
        f"touch {units}/ws-rescue.service",
        f"echo x >> {units}/ws-upgrade.service",
        f"touch {tool}/bin/ws",
    ]
    assert [exit_code(c) for c in refused] == [2] * len(refused)
    assert (
        exit_code(f"echo v0.0.0 > {home}/.local/state/ws-upgrade/request.json") == 0
    )  # the request path stays writable
    assert exit_code("ls -la") == 0
    assert exit_code(f"touch {units}/ws-other.service") == 0  # only the four units are named, not the folder


@pytest.mark.parametrize(
    "where",
    [
        ".config/systemd/user/ws-upgrade.service.d/override.conf",  # a blank ExecStart= plus its own command
        ".config/systemd/user/ws-upgrade.path.d/override.conf",
        ".config/systemd/user/ws-rescue.service.d/override.conf",
        ".config/systemd/user/ws-rescue.timer.d/override.conf",
        ".config/systemd/user/ws-.service.d/override.conf",  # the shared-prefix drop-in reaches both services
        ".config/systemd/user/service.d/override.conf",  # and the type's reaches every service
        ".config/systemd/user/timer.d/override.conf",
        ".config/systemd/user.control/ws-upgrade.service",  # what `systemctl edit` writes: read before the user folder
        ".config/systemd/user.control/ws-rescue.timer.d/override.conf",
        ".config/systemd/user.control/anything-else.conf",
        ".local/share/systemd/user/ws-upgrade.service.d/override.conf",
        ".local/share/systemd/user/ws-rescue.timer",
    ],
)
def test_no_drop_in_or_control_copy_of_a_unit_can_be_written(box, where):
    home, _tool = box
    assert exit_code(f"touch {home}/{where}") == 2, where
    assert exit_code(f"echo x >> {home}/{where}") == 2, where


def test_other_units_and_their_folders_stay_writable(box):
    home, _tool = box
    for where in (
        ".config/systemd/user/ws-other.service",
        ".config/systemd/user/ws-other.service.d/override.conf",
        ".config/systemd/user/my-backup.timer",
        ".local/share/systemd/user/my-backup.service",
    ):
        assert exit_code(f"touch {home}/{where}") == 0, where


def test_protected_paths_follow_xdg_and_the_runtime_folder(box, monkeypatch, tmp_path):
    cfg, data, run = tmp_path / "c", tmp_path / "d", tmp_path / "r"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    got = {str(p) for p in upgradeunit.protected_paths(os.environ)}
    assert f"{cfg}/systemd/user/ws-upgrade.service.d" in got and f"{cfg}/systemd/user.control" in got
    assert f"{data}/systemd/user/ws-rescue.timer.d" in got and f"{run}/systemd/transient" in got
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")  # a relative value is ignored, as the specification says
    assert all(Path(p).is_absolute() for p in upgradeunit.protected_paths(os.environ))


def test_the_protected_paths_are_the_four_units_and_the_tool_folder(box):
    home, tool = box
    from ws import userconfig

    protect = userconfig._claude_protect(home / ".claude" / "settings.json")
    names = {Path(p).name for p in protect}
    assert set(upgradeunit.UNITS) <= names and str(tool) in protect


def test_the_upgrade_adds_both_default_guards_once(box):
    path = Path(os.environ["WS_HOME"]) / "policy.toml"
    text = path.read_text(encoding="utf-8")
    assert '"pane-keys"' in text and '"upgrade-service"' in text
    assert upgrade.migrate_policy(os.environ) == []  # a second run changes nothing


def test_the_guard_is_a_default_guard():
    from ws.guards import upgrade_service

    assert upgrade_service.NAME in upgrade.DEFAULT_GUARDS
