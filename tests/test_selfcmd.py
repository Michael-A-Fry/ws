"""Every place ws starts itself is isolated: a planted `ws/__main__.py` in the working directory (the project a session is
working in) never runs in place of the gate hook, the broker or a timer's job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ws import broker, policy, run, selfcmd
from ws.adapters import claude_code, opencode
from ws.roles import _schedule

PLANT = "import pathlib, sys\npathlib.Path(__file__).with_name('RAN').write_text('planted ws ran')\nprint('PLANTED')\n"


@pytest.fixture
def project(tmp_path):
    """A project folder with a `ws` package planted in it."""
    p = tmp_path / "project"
    (p / "ws").mkdir(parents=True)
    (p / "ws" / "__init__.py").write_text("")
    (p / "ws" / "__main__.py").write_text(PLANT)
    return p


def ran(project) -> bool:
    return (project / "ws" / "RAN").exists()


def test_the_command_is_this_python_isolated_running_ws():
    assert selfcmd.argv() == [sys.executable, "-I", "-m", "ws"]


def test_plain_dash_m_runs_the_planted_package_and_the_isolated_command_does_not(project):
    """The control: without -I the planted package wins, so the other half of this test proves something."""
    bare = subprocess.run(
        [sys.executable, "-m", "ws", "--help"], cwd=project, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert ran(project) and "PLANTED" in bare.stdout, "the control did not reproduce the planted package"
    (project / "ws" / "RAN").unlink()
    p = subprocess.run(
        [*selfcmd.argv(), "--help"], cwd=project, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert p.returncode == 0 and "usage" in p.stdout.lower() and "PLANTED" not in p.stdout
    assert not ran(project)


def hook_command(adapter_module, out) -> str:
    files = adapter_module.generate(policy.default_policy(), selfcmd.argv(), out, out / "policy.toml")
    settings = json.loads(files["settings.json"])
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_the_generated_claude_hook_does_not_run_a_planted_package(project, tmp_path):
    cmd = hook_command(claude_code, tmp_path / "gate")
    assert "-I" in cmd
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ} | {"WS_HOME": str(tmp_path / "wsh")}
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    p = subprocess.run(
        ["sh", "-c", cmd], input=json.dumps(payload), cwd=project, env=env, capture_output=True, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    assert not ran(project), "the planted ws ran as the gate hook"
    assert "PLANTED" not in p.stdout + p.stderr


def test_the_generated_opencode_plugin_carries_the_isolated_command(tmp_path):
    files = opencode.generate(policy.default_policy(), selfcmd.argv(), tmp_path / "gate", tmp_path / "policy.toml")
    plugin = next(v for k, v in files.items() if k.endswith(".js"))
    assert json.dumps(selfcmd.argv()) in plugin


def test_the_run_writes_the_isolated_command_into_the_gate_config():
    src = Path(run.__file__).read_text(encoding="utf-8")
    assert 'adapter.generate(pol, selfcmd.argv(), run_dir / "gate", policy_file)' in src
    assert '"-m", "ws"' not in src


def test_a_timer_without_ws_on_the_path_starts_the_isolated_command(monkeypatch):
    assert _schedule.ws_command({"PATH": "/nonexistent"}) == selfcmd.argv()


def test_no_module_spawns_plain_dash_m_ws():
    """Guard against a new self-spawn that forgets the isolation. (secrets.py's wall launcher is its own, with -P -s.)"""
    root = os.path.dirname(run.__file__)
    bad = []
    for d, _, names in os.walk(root):
        for n in names:
            if n.endswith(".py") and n not in ("selfcmd.py", "secrets.py"):
                text = (Path(d) / n).read_text(encoding="utf-8")
                if '"-m", "ws"' in text:
                    bad.append(n)
    assert bad == []


def test_the_broker_ws_starts_runs_isolated_in_ws_home_with_a_scrubbed_environment(tmp_path, monkeypatch):
    home = tmp_path / "wsh"
    monkeypatch.setenv("WS_HOME", str(home))
    monkeypatch.setenv("SECRET_THING", "x")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, **kw)

    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(broker, "status", lambda socket=None, timeout=3.0: {"leases": 0})
    sock = home / "run" / "broker.sock"
    assert run._start_broker(sock) is True
    assert seen["argv"] == [*selfcmd.argv(), "broker", "serve", "--socket", str(sock)]
    assert seen["cwd"] == home
    assert set(seen["env"]) <= {*run.PASS_ENV, "WS_HOME"} and seen["env"]["WS_HOME"] == str(home)
    assert seen["start_new_session"] is True
    log = home / "logs" / "broker-start.log"
    assert log.exists() and oct(log.stat().st_mode & 0o777) == "0o600"


def test_a_real_broker_started_from_a_folder_with_a_planted_package_does_not_run_it(tmp_path, project, monkeypatch):
    home = tmp_path / "wsh"
    home.mkdir()
    monkeypatch.setenv("WS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    monkeypatch.chdir(project)
    procs = []
    real = subprocess.Popen

    def spy(*a, **kw):
        procs.append(real(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(run.subprocess, "Popen", spy)
    sock = tmp_path / "b.sock"  # short path: a Unix socket path tops out near 100 characters
    monkeypatch.setattr(run, "BROKER_START_S", 20.0)
    try:
        started = run._start_broker(sock)
        assert not ran(project), "the planted ws ran as the broker"
        assert started, (home / "logs" / "broker-start.log").read_text()
    finally:
        for pr in procs:
            pr.terminate()
            pr.wait(timeout=30)
