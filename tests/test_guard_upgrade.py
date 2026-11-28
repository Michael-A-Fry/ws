"""The upgrade-service guard: a session can't start, stop, change or imitate the unattended upgrade, whatever route it
takes to the unit names, and ordinary systemctl and bus use on other units still goes through."""

from __future__ import annotations

import pytest

from ws import guards
from ws.adapters import Call
from ws.guards import upgrade_service

CTX = guards.Context("claude-code", "pre", "s1", "/w", {})


def ask(command: str):
    return upgrade_service.judge(Call("command", command), None, CTX)


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user start ws-upgrade.service",
        "systemctl --user restart ws-upgrade.timer",
        "systemctl --user stop ws-upgrade.timer",
        "systemctl --user enable --now ws-upgrade.timer",
        "systemctl --user disable ws-upgrade.service",
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
        "ws upgrade --unatt",
        "ws upgrade --unattended=1",
        "python -m ws upgrade --unattended",
        "uv run ws upgrade --unattended",
        "env FOO=1 ws upgrade --unattended",
        "bash -c 'ws upgrade --unattended'",
        "/usr/bin/systemctl --user start ws-upgrade.service",
        "echo ws upgrade --unattended",  # the words of a command line are read, so a plain echo of them is refused too
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
        "systemctl --user list-timers ws-upgrade.timer",
        "systemctl --user is-active ws-upgrade.timer",
        "systemctl --user cat ws-upgrade.service",
        "systemctl --user show ws-upgrade.timer",
        "systemctl --user start ws-other.service",
        "systemctl --user restart some.service",
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


# ---- B9: the routes round the unit names, one planted each, each refused for its own reason ------------------------------------


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("systemctl --user stop 'ws-r*.timer'", "a glob or a brace"),
        ("systemctl --user stop ws-?escue.timer", "a glob or a brace"),
        ("systemctl --user stop 'ws-[r]escue.timer'", "a glob or a brace"),
        ("systemctl --user restart '*.timer'", "a glob or a brace"),
        ("systemctl --user stop -- 'ws-*'", "a glob or a brace"),
        ("systemctl --global disable 'ws-*'", "a glob or a brace"),
        ("systemctl -M 1000@.host stop 'ws-r*'", "a glob or a brace"),
        (
            "systemctl --user stop ws-{upgrade,other}.timer",
            "on the upgrade unit",
        ),  # the reader expands braces
        ("echo ws-upgrade.timer | xargs systemctl --user stop", "fed in by xargs"),
        ("xargs -a units systemctl --user disable", "fed in by xargs"),
        ("cat units | parallel systemctl --user stop", "fed in by xargs or parallel"),
        ("echo ws-upgrade.timer | xargs -I{} systemctl --user stop {}", "a glob or a brace"),
        ("find . -exec systemctl --user stop ws-upgrade.timer ';'", "on the upgrade unit"),
        ("watch systemctl --user restart ws-upgrade.timer", "on the upgrade unit"),
        ("systemctl --user daemon-reload", "systemctl daemon-reload"),
        ("systemctl daemon-reexec", "systemctl daemon-reexec"),
        ("systemctl --user set-environment PYTHONPATH=/tmp/x", "systemctl set-environment"),
        ("systemctl --user import-environment PYTHONPATH", "systemctl import-environment"),
        ("systemctl --user unset-environment PATH", "systemctl unset-environment"),
        ("sudo systemctl --user daemon-reload", "systemctl daemon-reload"),
        ("dbus-update-activation-environment --systemd PYTHONPATH=/tmp/x", "dbus-update-activation-environment"),
        (
            (
                "busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager "
                "StopUnit ss ws-upgrade.timer replace"
            ),
            "busctl calling the systemd user manager",
        ),
        (
            (
                "busctl --user set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager "
                "Environment as 1 PYTHONPATH=/tmp/x"
            ),
            "busctl calling the systemd user manager",
        ),
        (
            (
                "gdbus call --session --dest org.freedesktop.systemd1 --object-path /org/freedesktop/systemd1 "
                "--method org.freedesktop.systemd1.Manager.StopUnit ws-upgrade.timer replace"
            ),
            "gdbus calling the systemd user manager",
        ),
        (
            (
                "dbus-send --session --dest=org.freedesktop.systemd1 /org/freedesktop/systemd1 "
                "org.freedesktop.systemd1.Manager.Reload"
            ),
            "dbus-send calling the systemd user manager",
        ),
    ],
)
def test_every_route_round_the_unit_names_is_refused_for_its_own_reason(command, why):
    found = ask(command)
    assert found is not None and found.action == "deny", command
    assert why in found.reason, (command, found.reason)


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user restart foo.service",
        "systemctl --user stop some-other.timer",
        "systemctl --user enable --now my-backup.timer",
        "systemctl --user start foo@bar",
        "systemctl --user kill --signal=HUP foo.service",
        "systemctl --user status 'ws-*'",  # reading takes a glob
        "systemctl --user list-units 'ws-*'",
        "systemctl --user show -p Environment ws-upgrade.service",
        "systemctl --user show-environment",
        "systemctl --user is-active foo.service | xargs echo",
        "systemctl stop 'nginx*'",  # the system manager: not this guard's business
        "systemctl --no-pager list-timers",
        "busctl --user tree org.freedesktop.systemd1",
        "busctl --user get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager Version",
        "busctl --user call org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop org.freedesktop.DBus.Peer Ping",
        (
            "gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications "
            "--method org.freedesktop.Notifications.GetServerInformation"
        ),
        (
            "dbus-send --session --dest=org.freedesktop.Notifications /org/freedesktop/Notifications "
            "org.freedesktop.Notifications.GetServerInformation"
        ),
        "dbus-update-activation-environment DISPLAY",  # without --systemd it only reaches the bus's own activation
        "find . -name '*.service' -exec grep -l ws-upgrade {} +",
        "grep -r systemctl docs",
        "man systemctl",
    ],
)
def test_ordinary_systemctl_and_bus_use_on_other_units_still_goes_through(command):
    assert ask(command) is None, command


def test_the_unattended_flag_is_matched_whole_and_in_any_spelling_the_parser_takes():
    for command in (
        "ws upgrade --unattended",
        "ws --unattended upgrade",
        "ws upgrade --version v1 --unattended",
        "ws -- upgrade --unattended",
    ):
        found = ask(command)
        assert found is not None and "ws upgrade --unattended" in found.reason, command
    for command in ("ws upgrade --unattended-not-a-flag-ws-has", "ws upgrade --u", "ws upgrade --unix"):
        assert ask(command) is None, command  # no flag of ws's, and the parser takes none of them (allow_abbrev=False)


def test_the_guard_takes_no_options():
    with pytest.raises(guards.PolicyError):
        upgrade_service.config({"x": 1}, "[guards.upgrade-service]")
