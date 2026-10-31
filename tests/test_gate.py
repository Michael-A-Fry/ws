"""ws gate: init, generate (idempotent, never into a runner's own config), check (exit 0 allow / 2 deny, fail closed).
The real runners are in tests/e2e; here the generated files and the hook entry are checked on their own."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys

import pytest

from ws import adapters, cli, policy

PUSH = "git push --force origin main"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def write_policy(home, text: str):
    p = home / "wshome" / "policy.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def generate(runner, out, *extra):
    return cli.main(["gate", "generate", "--runner", runner, "--out", str(out), *extra])


def check(monkeypatch, capsys, runner, payload) -> tuple[int, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = cli.main(["gate", "check", "--runner", runner])
    return code, capsys.readouterr().err


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


# --- init --------------------------------------------------------------------------------------------------------


def test_init_writes_the_default_policy_once(home, capsys):
    assert cli.main(["gate", "init"]) == 0
    assert "wrote" in capsys.readouterr().out
    path = home / "wshome" / "policy.toml"
    assert path.read_text() == policy.default_toml()
    assert policy.load(path) == policy.default_policy()
    assert cli.main(["gate", "init"]) == 0
    assert "0 changes" in capsys.readouterr().out


def test_init_keeps_a_policy_you_edited_and_reports_a_broken_one(home, capsys):
    p = write_policy(home, 'dial = "allowlist"\n')
    assert cli.main(["gate", "init"]) == 0
    assert p.read_text() == 'dial = "allowlist"\n'
    p.write_text('dial = "yolo"\n')
    assert cli.main(["gate", "init"]) == 1
    assert "dial" in capsys.readouterr().err


def test_the_default_policy_file_is_commented_and_round_trips():
    text = policy.default_toml()
    assert text.count("\n#") > 15 and "Rule of Two" in text
    assert "rm -* /" in text  # the rules are written out, not hidden in code


# --- generate ----------------------------------------------------------------------------------------------------


def test_claude_code_settings(home, tmp_path, capsys):
    out = tmp_path / "out"
    assert generate("claude-code", out, "--ws-cmd", "uv run ws") == 0
    conf = json.loads((out / "settings.json").read_text())
    deny = conf["permissions"]["deny"]
    assert "Bash(git push*--force*)" in deny and "Bash(rm -* /)" in deny
    assert "Read(~/.ssh/**)" in deny
    assert not any("\\*" in d for d in deny)  # no runner has a literal star: those rules are hook-only
    hook = conf["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert hook["hooks"][0]["command"] == "uv run ws gate check --runner claude-code || exit 2"


def test_claude_code_rule_syntax_for_paths_and_tools(home, tmp_path):
    write_policy(
        home, '[gate]\ndeny_read = ["/etc/shadow", "~/x/**", ".env"]\ndeny_tools = ["web_fetch", "web_search"]\n'
    )
    out = tmp_path / "out"
    assert generate("claude-code", out) == 0
    deny = json.loads((out / "settings.json").read_text())["permissions"]["deny"]
    assert deny == ["Read(//etc/shadow)", "Read(~/x/**)", "Read(.env)", "WebFetch", "WebSearch"]


def test_opencode_config_and_shim(home, tmp_path):
    write_policy(
        home,
        '[gate]\ndeny_commands = ["make deploy", "rm -rf /x"]\ndeny_read = ["~/.ssh/**"]\ndeny_tools = ["web_fetch"]\n',
    )
    out = tmp_path / "out dir"
    assert generate("opencode", out, "--ws-cmd", "'/opt/my ws/bin/ws'") == 0
    conf = json.loads((out / "opencode.json").read_text())
    perm = conf["permission"]
    assert next(iter(perm["bash"])) == "*" and perm["bash"]["*"] == "allow"  # last match wins: the catch-all is first
    assert perm["bash"]["make deploy"] == "deny" and perm["bash"]["rm -fr /x"] == "deny"
    assert perm["read"] == {"*": "allow", "~/.ssh/**": "deny"}
    assert perm["webfetch"] == "deny" and "websearch" not in perm
    shim = (out / "plugins" / "ws-gate.js").read_text()
    assert "__WS_CMD__" not in shim
    assert 'const WS = ["/opt/my ws/bin/ws"]' in shim
    assert "tool.execute.before" in shim and "throw new Error" in shim


@pytest.mark.parametrize("runner", ["claude-code", "opencode"])
def test_generate_twice_is_zero_changes_and_follows_the_policy(home, tmp_path, capsys, runner):
    out = tmp_path / "out"
    assert generate(runner, out) == 0
    assert "changes to" in capsys.readouterr().out
    first = {p: p.read_text() for p in out.rglob("*") if p.is_file()}
    assert generate(runner, out) == 0
    msg = capsys.readouterr().out
    assert "0 changes" in msg
    assert {p: p.read_text() for p in out.rglob("*") if p.is_file()} == first
    write_policy(home, '[gate]\ndeny_commands = ["make deploy"]\n')
    assert generate(runner, out) == 0
    assert "0 changes" not in capsys.readouterr().out
    assert generate(runner, out) == 0
    assert "0 changes" in capsys.readouterr().out


@pytest.mark.parametrize("sub", [".claude", ".config/opencode", ".claude/hooks"])
def test_generate_never_writes_into_a_runners_own_config(home, tmp_path, capsys, sub):
    out = tmp_path / "home" / sub
    assert generate("claude-code", out) == 1
    assert "runner's own config" in capsys.readouterr().err
    assert not out.exists()


def test_generate_reports_a_broken_policy_and_an_unknown_runner(home, tmp_path, capsys):
    write_policy(home, "dial = 3\n")
    assert generate("claude-code", tmp_path / "out") == 1
    assert "dial" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        generate("emacs", tmp_path / "out")
    assert e.value.code == 2


def test_a_runner_is_one_module(home):
    found = adapters.all_adapters()
    assert set(found) == {"claude-code", "opencode"}
    for name, mod in found.items():
        assert mod.NAME == name and callable(mod.generate) and callable(mod.parse) and callable(mod.launch_hint)
    with pytest.raises(KeyError, match="unknown runner"):
        adapters.get("emacs")


# --- check -------------------------------------------------------------------------------------------------------


def test_check_allows_and_denies_claude_code_payloads(home, monkeypatch, capsys):
    assert check(monkeypatch, capsys, "claude-code", bash("ls -la")) == (0, "")
    code, err = check(monkeypatch, capsys, "claude-code", bash(f"echo hi && {PUSH}"))
    assert code == 2 and "blocked" in err and "git push*--force*" in err
    code, err = check(
        monkeypatch, capsys, "claude-code", {"tool_name": "Read", "tool_input": {"file_path": "~/.ssh/id_rsa"}}
    )
    assert code == 2 and "deny_read" in err
    assert (
        check(monkeypatch, capsys, "claude-code", {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}})[0] == 0
    )


def test_check_denies_a_tool_in_deny_tools_and_ignores_tools_it_does_not_govern(home, monkeypatch, capsys):
    write_policy(home, '[gate]\ndeny_tools = ["web_fetch"]\n')
    call = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
    assert check(monkeypatch, capsys, "claude-code", call)[0] == 2
    call = {"tool_name": "WebSearch", "tool_input": {"query": "x"}}
    assert check(monkeypatch, capsys, "claude-code", call)[0] == 0
    assert check(monkeypatch, capsys, "claude-code", {"tool_name": "Edit", "tool_input": {"file_path": "x"}})[0] == 0


def test_check_opencode_payloads(home, monkeypatch, capsys):
    ok = {"tool": "bash", "args": {"command": "ls"}}
    assert check(monkeypatch, capsys, "opencode", ok) == (0, "")
    code, err = check(monkeypatch, capsys, "opencode", {"tool": "bash", "args": {"command": PUSH}})
    assert code == 2 and "blocked" in err
    assert check(monkeypatch, capsys, "opencode", {"tool": "read", "args": {"filePath": "~/.ssh/id_rsa"}})[0] == 2
    assert check(monkeypatch, capsys, "opencode", {"tool": "read", "args": {"filePath": "/tmp/x"}})[0] == 0
    assert check(monkeypatch, capsys, "opencode", {"tool": "glob", "args": {"pattern": "*"}})[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        "null",
        "{}",
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": {"command": 7}},
        {"tool_name": 5, "tool_input": {}},
        {"tool_name": "Read", "tool_input": {"file_path": ""}},
    ],
)
def test_check_fails_closed_on_a_payload_it_cannot_read(home, monkeypatch, capsys, payload):
    code, err = check(monkeypatch, capsys, "claude-code", payload)
    assert code == 2 and "blocked to be safe" in err


def test_check_fails_closed_on_a_broken_policy_and_on_a_crash(home, monkeypatch, capsys):
    write_policy(home, "dial = [\n")
    assert check(monkeypatch, capsys, "claude-code", bash("ls"))[0] == 2
    write_policy(home, "")

    def boom(*a):
        raise RuntimeError("matcher bug")

    monkeypatch.setattr(policy, "is_denied_command", boom)
    code, err = check(monkeypatch, capsys, "claude-code", bash("ls"))
    assert code == 2 and "RuntimeError" in err


def test_check_never_echoes_the_command_it_blocked(home, monkeypatch, capsys):
    secret = "tok_" + "x1y2z3" * 3  # built at run time
    code, err = check(monkeypatch, capsys, "claude-code", bash(f"{PUSH} {secret}"))
    assert code == 2 and secret not in err


# --- the hook command as the runner runs it ----------------------------------------------------------------------


def hook_command(out) -> str:
    return json.loads((out / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def run_hook(command: str, payload: dict, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, shell=True, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60, check=False
    )


def test_the_generated_hook_command_allows_denies_and_fails_closed(home, tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert generate("claude-code", out, "--ws-cmd", shlex.join([sys.executable, "-m", "ws"])) == 0
    env = {"PATH": "/usr/bin:/bin", "WS_HOME": str(home / "wshome"), "HOME": str(home / "home")}
    cmd = hook_command(out)
    assert run_hook(cmd, bash("ls"), env).returncode == 0
    denied = run_hook(cmd, bash(PUSH), env)
    assert denied.returncode == 2 and "blocked" in denied.stderr
    # ws missing, or crashing on its own: the shell command still ends in 2, which blocks
    broken = cmd.replace(sys.executable, str(tmp_path / "no-such-ws"))
    assert run_hook(broken, bash("ls"), env).returncode == 2
    crashing = f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(1)' gate check || exit 2"
    assert run_hook(crashing, bash("ls"), env).returncode == 2
