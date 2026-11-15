"""`ws secret ask`, `wait` and the prompt window: the value never reaches argv, the environment, a marker or any output."""

from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from ws import agentsession, secretportal
from ws import secrets as wsecrets
from ws.output import WsError

VALUE = "planted-" + os.urandom(12).hex()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    h.mkdir()
    monkeypatch.setenv("WS_HOME", str(h))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    cfg = wsecrets.registry_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[demo]\nhosts = ["api.example.com"]\n')
    return h


def _ns(name, **kw):
    return SimpleNamespace(name=name, json=False, timeout=kw.pop("timeout", 2.0), **kw)


def _tree_text(root):
    out = []
    for p in root.rglob("*"):
        if p.is_file() and p.parent.name != "secrets":
            out.append(p.read_text(errors="replace"))
    return "\n".join(out)


def test_ask_opens_a_window_whose_command_has_the_name_and_no_value(home, monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setattr(secretportal.shutil, "which", lambda n: "/usr/bin/tmux")

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(secretportal.subprocess, "run", fake_run)
    assert secretportal.cmd_ask(_ns("demo")) == 0
    assert seen and seen[0][:2] == ["/usr/bin/tmux", "new-window"]
    cmd = seen[0][-1]
    assert "secret prompt demo" in cmd
    assert VALUE not in " ".join(seen[0])
    assert VALUE not in capsys.readouterr().out
    assert secretportal._read_marker("demo")["state"] == "asked"


def test_ask_refuses_an_unregistered_name_and_no_tmux(home, monkeypatch):
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setattr(secretportal.shutil, "which", lambda n: "/usr/bin/tmux")
    with pytest.raises(WsError) as e:
        secretportal.cmd_ask(_ns("nope"))
    assert e.value.kind == "not_found"
    monkeypatch.delenv("TMUX")
    with pytest.raises(WsError) as e:
        secretportal.cmd_ask(_ns("demo"))
    assert e.value.kind == "unsupported"


def test_prompt_stores_the_value_with_no_echo_and_leaves_no_trace(home, monkeypatch, capsys):
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": VALUE)
    assert secretportal.cmd_prompt(_ns("demo")) == 0
    f = wsecrets.store_dir() / "demo"
    assert f.read_text() == VALUE
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert VALUE not in out.out + out.err
    assert VALUE not in _tree_text(home)
    assert secretportal._read_marker("demo")["state"] == "stored"


def test_wait_returns_once_stored_and_fails_on_cancel_failure_and_timeout(home, monkeypatch, capsys):
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    with pytest.raises(WsError) as e:
        secretportal.cmd_wait(_ns("demo"))
    assert e.value.kind == "not_found"
    secretportal._write_marker("demo", "asked")
    with pytest.raises(WsError) as e:
        secretportal.cmd_wait(_ns("demo", timeout=0))
    assert e.value.kind == "timeout"
    secretportal._write_marker("demo", "cancelled")
    with pytest.raises(WsError) as e:
        secretportal.cmd_wait(_ns("demo"))
    assert e.value.kind == "failed"
    secretportal._write_marker("demo", "stored")
    assert secretportal.cmd_wait(_ns("demo")) == 0
    assert "demo is stored" in capsys.readouterr().out


def test_prompt_cancel_and_a_bad_value_are_recorded_not_stored(home, monkeypatch):
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)

    def cancel(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", cancel)
    assert secretportal.cmd_prompt(_ns("demo")) == 1
    assert secretportal._read_marker("demo")["state"] == "cancelled"
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "bad\x01value")
    assert secretportal.cmd_prompt(_ns("demo")) == 1
    assert secretportal._read_marker("demo")["state"] == "failed"
    assert "bad" not in json.dumps(secretportal._read_marker("demo"))
    assert not (wsecrets.store_dir() / "demo").exists()


def test_a_bad_name_never_reaches_a_path(home):
    with pytest.raises(WsError):
        secretportal.cmd_wait(_ns("../x"))


def test_nothing_in_src_docs_or_tests_reads_the_old_registry_file():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    needle = "provision/" + "secrets.tsv"
    hits = [
        str(p.relative_to(root))
        for d in ("src", "docs", "tests")
        for p in (root / d).rglob("*")
        if p.is_file()
        and p.suffix in (".py", ".md", ".toml")
        and p != Path(__file__).resolve()
        and needle in p.read_text(errors="ignore")
    ]
    assert hits == []


def test_a_gated_session_may_run_ask_and_wait_but_the_prompt_stays_for_a_person():
    for sub in ("ask", "wait"):
        assert agentsession.admin_name(f"secret.{sub}", SimpleNamespace()) is None
    assert agentsession.admin_name("secret.prompt", SimpleNamespace()) == "secret"
    under_a_runner = ["claude", "bash"]
    env = {agentsession.ENV_MARKER: "1"}
    for sub, code in (("ask", None), ("wait", None), ("prompt", 3), ("set", 3)):
        ns = SimpleNamespace(ws_command=f"secret.{sub}")
        try:
            agentsession.guard(ns, env=env, stdin_tty=False, stdout_tty=False, ancestry=lambda: under_a_runner)
            got = None
        except WsError as e:
            got = e.exit_code
        assert got == code, sub


def test_wait_stops_when_the_prompt_window_was_closed(home, monkeypatch):
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    secretportal._write_marker("demo", "asked")
    monkeypatch.setattr(secretportal, "STALE_AFTER_S", -1.0)
    monkeypatch.setattr(secretportal, "_window_gone", lambda n: True)
    with pytest.raises(WsError, match="was closed"):
        secretportal.cmd_wait(_ns("demo"))
    monkeypatch.setattr(secretportal, "_window_gone", lambda n: False)
    with pytest.raises(WsError) as e:
        secretportal.cmd_wait(_ns("demo", timeout=0))
    assert e.value.kind == "timeout"


def test_window_gone_reads_tmux_window_names(monkeypatch):
    monkeypatch.setattr(secretportal.shutil, "which", lambda n: "/usr/bin/tmux")

    def run(out, code=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(secretportal.subprocess, "run", run("main\nsecret:demo\n"))
    assert secretportal._window_gone("demo") is False
    monkeypatch.setattr(secretportal.subprocess, "run", run("main\n"))
    assert secretportal._window_gone("demo") is True
    monkeypatch.setattr(secretportal.subprocess, "run", run("", 1))  # tmux not answering: do not guess
    assert secretportal._window_gone("demo") is False


def test_the_prompt_window_says_where_the_value_will_go(home, monkeypatch, capsys):
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": VALUE)
    secretportal.cmd_prompt(_ns("demo"))
    out = capsys.readouterr().out
    assert str(wsecrets.store_dir()) in out and VALUE not in out
