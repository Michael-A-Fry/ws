"""The upgrade-service guard and the protected paths: a session can't start, stop, change or imitate the unattended
upgrade, and can still write the request file. The hook probes run the installed PreToolUse command, as a watchdog does."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ws import cli, guards, selfcmd, upgrade, upgradecheck, upgradeunit
from ws.adapters import Call
from ws.guards import upgrade_service

CTX = guards.Context("claude-code", "pre", "s1", "/w", {})


def ask(command: str):
    return upgrade_service.judge(Call("command", command), None, CTX)


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user start ws-upgrade.service",
        "systemctl --user restart ws-upgrade.path",
        "systemctl --user stop ws-rescue.timer",
        "systemctl --user enable --now ws-upgrade.path ws-rescue.timer",
        "systemctl --user disable ws-rescue.service",
        "systemctl --user edit ws-upgrade.service",
        "systemctl --user kill ws-upgrade",
        "systemctl --user mask ws-upgrade.service",
        "systemctl --user -T start WS-Upgrade.service",
        "systemctl --user start ws-upgrade@x",
        "systemctl --user start $UNIT",
        "systemctl --user start `cat unit`",
        "U=ws-upgrade.service; systemctl --user start $U",
        "sudo systemctl --user start ws-upgrade.service",
        "bash -c 'systemctl --user start ws-upgrade.service'",
        "systemd-run --user --unit=ws-upgrade-now true",
        "systemd-run --user ws upgrade --unattended",
        "ws upgrade --unattended",
        "ws upgrade --rescue",
        "ws upgrade --unatt",
        "ws upgrade --resc",
        "ws upgrade --unattended=1",
        "python -m ws upgrade --unattended",
        "uv run ws upgrade --rescue",
        "env FOO=1 ws upgrade --unattended",
        "bash -c 'ws upgrade --unattended'",
        "/usr/bin/systemctl --user start ws-upgrade.service",
        "echo ws upgrade --rescue",  # the words of a command line are read, so a plain echo of them is refused too
    ],
)
def test_the_service_is_not_for_a_session(command):
    found = ask(command)
    assert found is not None and found.action == "deny", command
    assert "outside every session" in found.reason


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user status ws-upgrade.service",
        "systemctl --user list-timers ws-rescue.timer",
        "systemctl --user is-active ws-upgrade.path",
        "systemctl --user cat ws-upgrade.service",
        "systemctl --user show ws-rescue.timer",
        "systemctl --user start ws-other.service",
        "systemctl --user restart some.service",
        "systemctl --user daemon-reload",
        "systemctl restart $svc",  # a system unit by a variable: not this guard's business
        "systemd-run --user sleep 1",
        "ws upgrade --dry-run",
        "ws upgrade --version v1.2.3 --check true",
        "ws upgrade --hooks-only",
        "ws upgrade --rollback",
        "ws upgrade --u",  # argparse reads --u as ambiguous; too short to be a flag
        "git commit -m 'ws upgrade --unattended is guarded'",
        "grep -r unattended docs",
        "ls ~/.config/systemd/user",
    ],
)
def test_reading_the_units_and_every_other_command_goes_through(command):
    assert ask(command) is None, command


def test_the_guard_takes_no_options_and_is_a_default_guard():
    with pytest.raises(guards.PolicyError):
        upgrade_service.config({"x": 1}, "[guards.upgrade-service]")
    assert upgrade_service.NAME in upgrade.DEFAULT_GUARDS


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
