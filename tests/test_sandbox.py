"""The sandbox wall (ws.sandbox, and the `sandbox` block the claude-code adapter writes). What really stops a bypass is
in tests/e2e/test_sandbox.py; here the command lines, the profile text and the fail-closed paths are checked without
running a sandbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import cli, policy, sandbox
from ws.adapters import claude_code, opencode

POLICY = policy.loads(
    '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "$HOME/.gnupg/**", "~/.s*/id_rsa", "/"]\n'
)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".aws").mkdir()
    (h / ".aws" / "credentials").write_text("x")
    return h


@pytest.fixture
def wshome(tmp_path):
    w = tmp_path / "wshome"
    w.mkdir()
    return w


def which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- what gets hidden -------------------------------------------------------------------------------------------


def test_read_rules_become_paths_a_sandbox_can_hide():
    # a trailing ** goes; a wildcard in the middle, the root and a bare ~ are left to the matcher
    assert policy.sandbox_read_paths(POLICY) == ["~/.ssh", "~/.aws/credentials", "~/.gnupg"]
    assert (
        policy.sandbox_read_paths(policy.loads('[gate]\ndeny_read = ["~", "~/**", "**/.ssh/**", "relative"]\n')) == []
    )


def test_only_paths_that_exist_are_hidden_and_home_is_the_runners(home):
    got = sandbox.hidden_paths(POLICY, {"HOME": str(home)})
    assert got == [str(home / ".ssh"), str(home / ".aws" / "credentials")]  # no .gnupg here


# --- the Linux command line ---------------------------------------------------------------------------------------


def wrapped(home, wshome, **kw):
    kw.setdefault("which", which("bwrap"))
    kw.setdefault("probe", lambda: True)
    kw.setdefault("system", "linux")
    return sandbox.wrap(
        ["opencode", "run"], POLICY, wshome, [str(wshome / "policy.toml")], env={"HOME": str(home)}, **kw
    )


def test_bwrap_binds_ws_home_read_only_then_reopens_the_two_holes_then_hides_reads(home, wshome):
    (wshome / "policy.toml").write_text("")
    argv = wrapped(home, wshome, scratch=[str(wshome / "gate")], lock=[str(wshome / "gate" / "opencode.json")])
    assert argv[:4] == ["bwrap", "--bind", "/", "/"] and argv[-3:] == ["--", "opencode", "run"]
    text = " ".join(argv)
    ro, rw = f"--ro-bind {wshome} {wshome}", f"--bind {wshome}/approvals/pending {wshome}/approvals/pending"
    assert ro in text and rw in text and f"--bind {wshome}/sessions {wshome}/sessions" in text
    assert f"--ro-bind {wshome}/policy.toml {wshome}/policy.toml" in text
    assert text.index(ro) < text.index(rw)  # the hole is cut after the read-only mount, or it would be covered
    assert f"--tmpfs {home}/.ssh" in text and f"--ro-bind /dev/null {home}/.aws/credentials" in text
    assert "--dev-bind /dev /dev" in text and "--die-with-parent" in argv
    assert "--new-session" in argv  # no terminal: the escape through the controlling terminal is shut
    assert "--unshare-net" not in argv  # the runner needs the model's API
    assert "granted" not in text  # the folder that holds grants is never reopened


def test_a_scratch_folder_is_writable_and_its_lock_files_read_only_again(home, wshome):
    (wshome / "gate").mkdir()
    (wshome / "gate" / "opencode.json").write_text("{}")
    argv = wrapped(home, wshome, scratch=[str(wshome / "gate")], lock=[str(wshome / "gate" / "opencode.json")])
    text = " ".join(argv)
    scratch, lock = f"--bind {wshome}/gate {wshome}/gate", f"--ro-bind {wshome}/gate/opencode.json"
    assert text.index(scratch) < text.index(lock)


def test_an_interactive_runner_keeps_its_terminal(home, wshome):
    assert "--new-session" not in wrapped(home, wshome, tty=True)


def test_the_holes_exist_before_the_mount(home, wshome):
    wrapped(home, wshome)
    assert (wshome / "approvals" / "pending").is_dir() and (wshome / "sessions").is_dir()


# --- fail closed --------------------------------------------------------------------------------------------------


def test_no_bubblewrap_refuses_with_the_install_line(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="apt install bubblewrap"):
        wrapped(home, wshome, which=which())


def test_a_bubblewrap_that_cannot_start_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="cannot start"):
        wrapped(home, wshome, probe=lambda: False)


def test_another_system_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="no sandbox for windows"):
        wrapped(home, wshome, system="windows")


def test_macos_without_sandbox_exec_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="sandbox-exec"):
        wrapped(home, wshome, system="darwin", which=which())


def test_macos_profile_denies_writes_to_ws_and_reads_of_the_hidden_paths(home, wshome):
    argv = wrapped(home, wshome, system="darwin", which=which("sandbox-exec"))
    assert argv[:2] == ["sandbox-exec", "-p"] and argv[3:] == ["opencode", "run"]
    prof = argv[2]
    real = str(wshome.resolve())
    assert "(allow default)" in prof and f'(deny file-write* (subpath "{real}"))' in prof
    assert f'(deny file-read* (subpath "{home.resolve()}/.ssh"))' in prof
    assert f'(deny file-read* (literal "{home.resolve()}/.aws/credentials"))' in prof
    assert prof.index(f'(deny file-write* (subpath "{real}"))') < prof.index(
        f'(allow file-write* (subpath "{real}/sessions"))'
    )


def test_the_profile_quotes_awkward_paths(tmp_path):
    odd = tmp_path / 'a "b" \\ c'
    prof = sandbox.sandbox_exec_profile(sandbox.Plan(odd))
    assert '\\"b\\"' in prof and "\\\\" in prof


# --- ws gate launch -----------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, wshome):
    monkeypatch.setenv("WS_HOME", str(wshome))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    started = []
    monkeypatch.setattr(sandbox.os, "execvpe", lambda exe, argv, e: started.append((argv, e)))
    return started


def test_launch_wraps_the_runner_and_marks_the_session(env, monkeypatch):
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: ["wall", *argv])
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode", "run"]) == 0
    argv, e = env[0]
    assert argv == ["wall", "opencode", "run"] and e["WS_GATED_SESSION"] == "1"


def test_launch_fails_closed_when_the_wall_cannot_be_built(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise sandbox.SandboxError("bubblewrap (bwrap) is not installed; install bubblewrap")

    monkeypatch.setattr(sandbox, "wrap", boom)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode"]) == 1
    err = capsys.readouterr().err
    assert "refusing to start" in err and "install bubblewrap" in err and "--no-sandbox" in err
    assert env == []  # the runner never started


def test_no_sandbox_starts_bare_and_says_what_it_gives_up(env, capsys):
    assert cli.main(["gate", "launch", "--runner", "opencode", "--no-sandbox", "--", "opencode"]) == 0
    err = capsys.readouterr().err
    assert "NO wall" in err and "python3 -c" in err and "policy" in err
    assert env[0][0] == ["opencode"] and env[0][1]["WS_GATED_SESSION"] == "1"


def test_launch_needs_a_command_and_does_not_wrap_claude_code(env, capsys):
    assert cli.main(["gate", "launch", "--runner", "opencode"]) == 1
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 1
    assert "own sandbox" in capsys.readouterr().err and env == []


def test_launch_locks_the_generated_files_but_leaves_the_runners_own_housekeeping(env, monkeypatch, tmp_path, wshome):
    out = wshome / "gate" / "opencode"
    out.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(out))
    seen = {}
    monkeypatch.setattr(sandbox, "wrap", lambda argv, pol, home, protect, **k: seen.update(k, protect=protect) or argv)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--protect", str(tmp_path / "extra"), "--", "x"]) == 0
    assert seen["scratch"] == [str(out)]
    assert seen["lock"] == [str(out / "opencode.json"), str(out / "plugins")]
    assert str(tmp_path / "extra") in seen["protect"] and str(wshome / "approvals") in seen["protect"]


# --- the Claude Code sandbox block --------------------------------------------------------------------------------


def claude_settings(tmp_path, toml: str = "") -> dict:
    pol = policy.loads(toml)
    out = tmp_path / "wshome" / "gate" / "claude-code"
    files = claude_code.generate(pol, ["ws"], out, tmp_path / "wshome" / "policy.toml")
    return json.loads(files["settings.json"])


def test_claude_code_settings_turn_the_sandbox_on_and_fail_closed(tmp_path):
    s = claude_settings(tmp_path, '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "~/x*/y"]\n')
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    deny = sb["filesystem"]["denyWrite"]
    wshome = str(tmp_path / "wshome")
    assert deny[0] == wshome and str(tmp_path / "wshome" / "policy.toml") in deny
    assert str(tmp_path / "wshome" / "gate" / "claude-code") in deny
    assert all(str(tmp_path / "wshome" / n) in deny for n in policy.CONTROL_NAMES)
    assert sb["filesystem"]["denyRead"] == ["~/.ssh", "~/.aws/credentials"]
    assert "network" not in sb  # the policy names no hosts: it says nothing about the network
    assert s["env"] == {"WS_GATED_SESSION": "1"}


def test_claude_code_network_follows_allow_hosts(tmp_path):
    s = claude_settings(tmp_path, '[gate]\nallow_hosts = ["api.anthropic.com", "*.npmjs.org"]\n')
    assert s["sandbox"]["network"] == {"allowedDomains": ["api.anthropic.com", "*.npmjs.org"]}


def test_only_documented_sandbox_keys_are_used(tmp_path):
    sb = claude_settings(tmp_path, '[gate]\nallow_hosts = ["a.b"]\n')["sandbox"]
    assert set(sb) <= {"enabled", "failIfUnavailable", "allowUnsandboxedCommands", "filesystem", "network"}
    assert set(sb["filesystem"]) <= {"allowWrite", "denyWrite", "denyRead", "allowRead"}


def test_the_opencode_shim_sends_the_session_and_folder(tmp_path):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    shim = files[opencode.PLUGIN]
    assert "session_id: input.sessionID" in shim and "cwd: directory" in shim
    assert "ws gate launch --runner opencode -- opencode" in opencode.launch_hint(str(tmp_path))
    assert Path(opencode.PLUGIN).parts[0] in opencode.WALL_PROTECT and opencode.CONFIG in opencode.WALL_PROTECT
