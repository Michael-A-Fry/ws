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
    text = brokerunit.render(sock, ["/opt/ws env/bin/python", "-I", "-m", "ws"], "/usr/bin")
    assert "Restart=always\n" in text and "StartLimitIntervalSec=0\n" in text and "Type=simple\n" in text
    assert f'ExecStart="/opt/ws env/bin/python" -I -m ws broker serve --socket {sock}\n' in text
    assert f"WorkingDirectory={tmp_path / 'state'}\n" in text and "Environment=PATH=/usr/bin\n" in text


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
    assert brokerunit.cmd_unit(argparse.Namespace(apply=False, remove=True), run=sc) == 0
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
