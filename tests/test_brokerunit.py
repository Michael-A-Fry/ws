"""The broker as a systemd user service (C848): the unit text, apply and remove, and that `ws run` and `ws upgrade` go
through systemd once the unit exists (24 Nov: a stopped bare broker took Telegram and inbox-v2 down)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from ws import brokerunit, run


class Systemctl:
    def __init__(self, rc: int = 0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append(argv[2:])
        return subprocess.CompletedProcess(argv, self.rc, "", "")


def test_the_unit_restarts_always_with_no_start_limit_and_runs_this_ws(tmp_path):
    sock = tmp_path / "state" / "run" / "broker.sock"
    env = {"HOME": str(tmp_path), "WS_HOME": str(tmp_path / "ws"), "PATH": "/usr/bin"}
    text = brokerunit.render(sock, ["/opt/ws env/bin/python", "-I", "-m", "ws"], env)
    assert "Restart=always\n" in text and "StartLimitIntervalSec=0\n" in text and "Type=simple\n" in text
    assert f'ExecStart="/opt/ws env/bin/python" -I -m ws broker serve --socket {sock}\n' in text
    assert f"WorkingDirectory={tmp_path / 'state'}\n" in text
    assert f"WS_HOME={tmp_path / 'ws'}" in text and "Environment=" in text  # the layout is pinned, as for a timer


@pytest.fixture(autouse=True)
def _person(monkeypatch):
    from ws import agentsession

    monkeypatch.setattr(agentsession, "refusal", lambda *a, **kw: None)
    monkeypatch.setattr(brokerunit.selfcmd, "unstable", lambda cmd=None: None)


def test_an_agent_session_may_not_apply_or_remove(tmp_path, monkeypatch):
    from ws import agentsession
    from ws.output import WsError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(agentsession, "refusal", lambda *a, **kw: "an agent's session")
    for flags in ({"apply": True, "remove": False}, {"apply": False, "remove": True}):
        with pytest.raises(WsError, match="agent"):
            brokerunit.cmd_unit(argparse.Namespace(**flags), run=Systemctl())
    assert not brokerunit.installed()


def test_an_unstable_ws_may_not_become_the_service(tmp_path, monkeypatch):
    from ws.output import WsError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(brokerunit.selfcmd, "unstable", lambda cmd=None: "a project's .venv")
    with pytest.raises(WsError, match="venv"):
        brokerunit.cmd_unit(argparse.Namespace(apply=True, remove=False), run=Systemctl())


def test_refresh_points_execstart_at_the_new_ws_and_reloads_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sc = Systemctl()
    assert brokerunit.refresh("/new/python", run=sc) and sc.calls == [["daemon-reload"]]
    assert "ExecStart=/new/python -I -m ws broker serve" in brokerunit.unit_path().read_text()
    assert brokerunit.refresh("/new/python", run=sc) and sc.calls == [["daemon-reload"]]  # unchanged: no reload


def test_remove_starts_a_bare_broker_and_a_failed_disable_is_an_error(tmp_path, monkeypatch):
    from ws.output import WsError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(brokerunit.shutil, "which", lambda name: "/usr/bin/systemctl")
    brokerunit.cmd_unit(argparse.Namespace(apply=True, remove=False), run=Systemctl())
    with pytest.raises(WsError, match="disable"):
        brokerunit.cmd_unit(
            argparse.Namespace(apply=False, remove=True), run=Systemctl(rc=1), start_bare=lambda s: True
        )
    assert brokerunit.installed()  # a unit systemd still runs is not deleted under it
    bare: list = []
    brokerunit.cmd_unit(
        argparse.Namespace(apply=False, remove=True), run=Systemctl(), start_bare=lambda s: bare.append(s) or True
    )
    assert bare and not brokerunit.installed()


def test_apply_writes_reloads_and_enables_and_remove_undoes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(brokerunit.shutil, "which", lambda name: "/usr/bin/systemctl")
    sc = Systemctl()
    assert not brokerunit.installed()
    assert brokerunit.cmd_unit(argparse.Namespace(apply=True, remove=False), run=sc) == 0
    path = tmp_path / "cfg" / "systemd" / "user" / "ws-broker.service"
    assert brokerunit.installed() and "Restart=always" in path.read_text()
    assert sc.calls == [["daemon-reload"], ["enable", "--now", "ws-broker.service"]]
    sc.calls.clear()
    assert brokerunit.cmd_unit(argparse.Namespace(apply=False, remove=True), run=sc, start_bare=lambda s: True) == 0
    assert not path.exists() and sc.calls == [["disable", "--now", "ws-broker.service"], ["daemon-reload"]]


def test_printing_the_unit_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sc = Systemctl()
    assert brokerunit.cmd_unit(argparse.Namespace(apply=False, remove=False), run=sc) == 0
    assert "Restart=always" in capsys.readouterr().out and sc.calls == [] and not brokerunit.installed()


def test_a_failed_systemctl_step_is_an_error(tmp_path, monkeypatch):
    from ws.output import WsError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(brokerunit.shutil, "which", lambda name: "/usr/bin/systemctl")
    with pytest.raises(WsError, match="daemon-reload"):
        brokerunit.cmd_unit(argparse.Namespace(apply=True, remove=False), run=Systemctl(rc=1))


def test_with_the_unit_installed_ws_run_starts_the_service_never_a_bare_broker(tmp_path, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(brokerunit, "installed", lambda env=None: True)
    monkeypatch.setattr(brokerunit, "start", lambda run=None: asked.append("start") or True)
    monkeypatch.setattr(run, "_broker_answers", lambda sock: True)

    def no_popen(*a, **kw):
        raise AssertionError("a bare broker was spawned beside the service")

    monkeypatch.setattr(run.subprocess, "Popen", no_popen)
    assert run._start_broker(Path(tmp_path / "b.sock")) is True and asked == ["start"]
    monkeypatch.setattr(brokerunit, "start", lambda run=None: False)
    assert run._start_broker(Path(tmp_path / "b.sock")) is False  # systemd refused: no fallback to a bare one
