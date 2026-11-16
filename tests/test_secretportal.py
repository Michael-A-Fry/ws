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
    # the account's own folders are the test's folders; the tests below that plant a different one say so
    monkeypatch.setattr(secretportal, "trusted_env", lambda: {"HOME": str(tmp_path), "WS_HOME": str(h)})
    monkeypatch.setattr(secretportal.selfcmd, "unstable", lambda cmd=None: None)
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


@pytest.fixture
def account(tmp_path, monkeypatch):
    """The account's own folders (no variable), and a session whose environment points them at its own folder."""
    acct = tmp_path / "acct"
    (acct / ".ws").mkdir(parents=True)
    (acct / ".ws" / "secrets.toml").write_text('[demo]\nhosts = ["api.example.com"]\n')
    monkeypatch.setattr(secretportal.paths, "account_home", lambda: acct)
    monkeypatch.setattr(secretportal.selfcmd, "unstable", lambda cmd=None: None)
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    for v in secretportal.paths.LAYOUT_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(acct))
    planted = tmp_path / "planted"
    (planted).mkdir()
    (planted / "secrets.toml").write_text('[demo]\nhosts = ["api.example.com"]\n')
    return acct, planted


def test_a_session_with_its_own_ws_folders_cannot_ask(account, monkeypatch):
    _, planted = account
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setattr(secretportal.shutil, "which", lambda n: "/usr/bin/tmux")
    for var, val in (("WS_HOME", str(planted)), ("HOME", str(planted))):
        with monkeypatch.context() as m:
            m.setenv(var, val)
            with pytest.raises(WsError) as e:
                secretportal.cmd_ask(_ns("demo"))
            assert e.value.kind == "refused", var


def test_the_window_runs_with_no_session_environment_and_only_the_account_home(account, monkeypatch):
    acct, planted = account
    monkeypatch.setenv("WS_HOME", str(planted))
    monkeypatch.setenv("XDG_DATA_HOME", str(planted))
    cmd = secretportal.window_command("demo")
    assert cmd.startswith("/usr/bin/env -i HOME=")
    assert str(planted) not in cmd and "WS_HOME" not in cmd and "XDG" not in cmd
    assert f"HOME={acct}" in cmd and "secret prompt demo" in cmd


def test_the_window_is_refused_from_a_ws_that_runs_from_a_folder_that_comes_and_goes(account, monkeypatch):
    monkeypatch.setattr(secretportal.selfcmd, "unstable", lambda cmd=None: "/x/.venv/bin/python is inside a .venv")
    with pytest.raises(WsError) as e:
        secretportal.window_command("demo")
    assert e.value.kind == "refused"


def test_the_prompt_takes_its_store_from_the_account_not_from_the_session_environment(account, monkeypatch):
    acct, planted = account
    monkeypatch.setenv(
        "WS_HOME", str(planted)
    )  # the session's variable, which the window never gets and which is ignored
    monkeypatch.setenv("HOME", str(planted))
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": VALUE)
    assert secretportal.cmd_prompt(_ns("demo")) == 0
    assert (acct / ".ws" / "secrets" / "demo").read_text() == VALUE
    assert not (planted / "secrets").exists()
    assert VALUE not in "\n".join(p.read_text(errors="replace") for p in planted.rglob("*") if p.is_file())


def test_the_prompt_honours_the_store_the_accounts_own_profile_names(account, monkeypatch, tmp_path):
    acct, planted = account
    chosen = tmp_path / "chosen-store"
    (acct / ".ws" / "profile.toml").write_text(f'[roles.secrets]\nstore = "{chosen}"\n')
    # a session's own profile in its own folder says nothing
    (planted / "profile.toml").write_text(f'[roles.secrets]\nstore = "{planted / "elsewhere"}"\n')
    monkeypatch.setenv("WS_HOME", str(planted))
    monkeypatch.setattr(secretportal.time, "sleep", lambda s: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": VALUE)
    assert secretportal.cmd_prompt(_ns("demo")) == 0
    assert (chosen / "demo").read_text() == VALUE
    assert not (planted / "elsewhere").exists()


def test_wait_reads_the_accounts_markers_not_the_sessions(account, monkeypatch):
    acct, planted = account
    marker = acct / ".ws" / "secret-asks" / "demo.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"name": "demo", "state": "stored"}))
    monkeypatch.setenv("WS_HOME", str(planted))  # a planted "stored" marker would be read if this were followed
    (planted / "secret-asks").mkdir()
    (planted / "secret-asks" / "demo.json").write_text(json.dumps({"name": "demo", "state": "cancelled"}))
    assert secretportal.cmd_wait(_ns("demo")) == 0


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
