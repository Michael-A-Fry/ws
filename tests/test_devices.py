"""`ws device add`: the join line, from a fake `tailscale status --json`. No network, no tailscale needed."""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from ws import cli, devices

HOST = "node1.example.com"


def status(**kw) -> str:
    base = {"BackendState": "Running", "Self": {"DNSName": HOST + ".", "HostName": "node1"}}
    return json.dumps({**base, **kw})


def add(capsys, run, which=lambda _: "/usr/bin/tailscale", name=None):
    code = devices.cmd_add(argparse.Namespace(name=name), run=run, which=which)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_prints_the_one_line_with_this_machines_name(capsys):
    calls = []

    def run(argv):
        calls.append(argv)
        return status()

    code, out, _ = add(capsys, run, name="laptop")
    assert code == 0
    assert calls == [["tailscale", "status", "--json"]]
    assert "On laptop, run:" in out
    assert out.rstrip().endswith(f"{devices.INSTALL_WS} && ws init --server {HOST}")


def test_falls_back_to_the_host_name(capsys):
    code, out, _ = add(capsys, lambda argv: status(Self={"HostName": "box"}))
    assert code == 0 and "--server box" in out and "the new machine" in out


def test_without_tailscale_says_it_is_needed_and_where_to_get_it(capsys):
    code, out, err = add(capsys, lambda argv: pytest.fail("must not run"), which=lambda _: None)
    assert code == 1 and out == ""
    assert "Tailscale is needed" in err and devices.INSTALL_URL in err


def test_tailscale_not_running_is_reported(capsys):
    code, _, err = add(capsys, lambda argv: status(BackendState="Stopped"))
    assert code == 1 and "not signed in and running" in err


def test_a_status_that_will_not_run_or_parse_is_reported(capsys):
    def boom(argv):
        raise subprocess.CalledProcessError(1, argv)

    code, _, err = add(capsys, boom)
    assert code == 1 and "did not answer" in err
    code, _, err = add(capsys, lambda argv: "not json")
    assert code == 1 and "did not answer" in err


def test_cli_registers_device_add(capsys, monkeypatch):
    monkeypatch.setattr(devices.shutil, "which", lambda _: None)
    assert cli.main(["device", "add", "--name", "x"]) == 1
    assert "Tailscale is needed" in capsys.readouterr().err
