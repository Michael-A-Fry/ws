"""ws gate: init, generate (idempotent, never into a runner's own config), check (exit 0 allow / 2 deny, fail closed).
The real runners are in tests/e2e; here the generated files and the hook entry are checked on their own."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from ws import adapters, cli, gate, policy

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


def check(monkeypatch, capsys, runner, payload, *extra) -> tuple[int, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = cli.main(["gate", "check", "--runner", runner, *extra])
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
    assert hook["matcher"].split("|") == [
        "Bash",
        "Read",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
    ]
    policy_file = (home / "wshome" / "policy.toml").resolve()
    # absolute paths: the hook reads this policy wherever the runner starts, and protects this folder
    assert hook["hooks"][0]["command"] == (
        f"uv run ws gate check --runner claude-code --policy {policy_file} --protect {out.resolve()} || exit 2"
    )
    assert f"Edit(/{policy_file})" in deny and f"Edit(/{out.resolve()}/**)" in deny  # a session cannot edit its gate
    wshome = (home / "wshome").resolve()
    for name in policy.CONTROL_NAMES:  # nor the rest of ws's control files, nor its run records
        assert f"Edit(/{wshome / name})" in deny and f"Edit(/{wshome / name}/**)" in deny, name


def test_claude_code_rule_syntax_for_paths_and_tools(home, tmp_path):
    write_policy(
        home, '[gate]\ndeny_read = ["/etc/shadow", "~/x/**", ".env"]\ndeny_tools = ["web_fetch", "web_search"]\n'
    )
    out = tmp_path / "out"
    assert generate("claude-code", out) == 0
    deny = json.loads((out / "settings.json").read_text())["permissions"]["deny"]
    assert deny[:3] == ["Read(//etc/shadow)", "Read(~/x/**)", "Read(.env)"]
    assert deny[-2:] == ["WebFetch", "WebSearch"]
    assert all(d.startswith("Edit(//") for d in deny[3:-2]) and len(deny[3:-2]) == 2 * (len(policy.CONTROL_NAMES) + 1)


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
    rules = {"*": "allow", "~/.ssh/**": "deny", "**/.ssh/**": "deny"}  # the second is how OpenCode sees a path
    assert perm["read"] == perm["grep"] == perm["glob"] == perm["list"] == rules
    wshome = (home / "wshome").resolve()
    assert perm["edit"]["*"] == "allow" and next(iter(perm["edit"])) == "*"
    for path in (out.resolve(), *(wshome / n for n in policy.CONTROL_NAMES)):
        assert perm["edit"][str(path)] == perm["edit"][f"{path}/**"] == "deny", path
    assert perm["webfetch"] == "deny" and "websearch" not in perm
    shim = (out / "plugins" / "ws-gate.js").read_text()
    assert "__CHECK__" not in shim
    argv = json.loads(shim.split("const CHECK = ")[1].split("\n")[0])
    assert argv[:4] == ["/opt/my ws/bin/ws", "gate", "check", "--runner"] and argv[4] == "opencode"
    assert argv[5:] == ["--policy", str((home / "wshome" / "policy.toml").resolve()), "--protect", str(out.resolve())]
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

    def boom(*a, **kw):
        raise RuntimeError("matcher bug")

    monkeypatch.setattr(policy, "is_denied_command", boom)
    code, err = check(monkeypatch, capsys, "claude-code", bash("ls"))
    assert code == 2 and "RuntimeError" in err


def test_check_never_echoes_the_command_it_blocked(home, monkeypatch, capsys):
    secret = "tok_" + "x1y2z3" * 3  # built at run time
    code, err = check(monkeypatch, capsys, "claude-code", bash(f"{PUSH} {secret}"))
    assert code == 2 and secret not in err


def test_check_covers_every_tool_the_claude_hook_matches(home, monkeypatch, capsys):
    ssh = str(home / "home" / ".ssh")
    cases = {
        2: [
            ("Grep", {"pattern": "x", "path": ssh}),
            ("Grep", {"pattern": "x", "path": "~/.ssh"}),
            ("Glob", {"pattern": "*", "path": "~/.ssh"}),
            ("Glob", {"pattern": "~/.ssh/*"}),
            ("Glob", {"pattern": "/**/id_rsa", "path": "/"}),
            ("Read", {"file_path": ssh + "/id_rsa"}),
        ],
        0: [
            ("Grep", {"pattern": "x"}),
            ("Grep", {"pattern": "x", "path": "src"}),
            ("Glob", {"pattern": "**/*.py"}),
            ("Glob", {"pattern": "*.md", "path": str(home / "home" / "proj")}),
            ("Read", {"file_path": str(home / "notes.txt")}),
        ],
    }
    for want, calls in cases.items():
        for name, inp in calls:
            code, err = check(monkeypatch, capsys, "claude-code", {"tool_name": name, "tool_input": inp})
            assert code == want, (name, inp, err)


def test_check_protects_ws_own_files_from_edit_tools_and_the_shell(home, monkeypatch, capsys):
    policy_file = home / "wshome" / "policy.toml"
    gate_dir = home / "gate-out"
    extra = ("--protect", str(gate_dir))
    for name, key in (
        ("Edit", "file_path"),
        ("Write", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ):
        for target in (policy_file, gate_dir / "settings.json", gate_dir / "plugins" / "ws-gate.js"):
            code, err = check(
                monkeypatch, capsys, "claude-code", {"tool_name": name, "tool_input": {key: str(target)}}, *extra
            )
            assert code == 2 and "protected" in err, (name, target)
        other = {"tool_name": name, "tool_input": {key: str(home / "project" / "a.py")}}
        assert check(monkeypatch, capsys, "claude-code", other, *extra)[0] == 0
    for tool in ("edit", "write", "multiedit"):
        payload = {"tool": tool, "args": {"filePath": str(policy_file)}}
        assert check(monkeypatch, capsys, "opencode", payload, *extra)[0] == 2
        payload = {"tool": tool, "args": {"filePath": str(home / "project" / "a.py")}}
        assert check(monkeypatch, capsys, "opencode", payload, *extra)[0] == 0
    for name in policy.CONTROL_NAMES:  # profile, secrets, jobs, release, runs, logs: not for the agent to write
        target = home / "wshome" / name / "x.toml"
        assert (
            check(monkeypatch, capsys, "claude-code", {"tool_name": "Write", "tool_input": {"file_path": str(target)}})[
                0
            ]
            == 2
        )
        assert check(monkeypatch, capsys, "claude-code", bash(f"echo x >> {target}"))[0] == 2
        assert check(monkeypatch, capsys, "opencode", {"tool": "write", "args": {"filePath": str(target)}})[0] == 2
    # the run record may be read, only writing is denied
    runs = home / "wshome" / "logs" / "runs.jsonl"
    assert (
        check(monkeypatch, capsys, "claude-code", {"tool_name": "Read", "tool_input": {"file_path": str(runs)}})[0] == 0
    )
    assert check(monkeypatch, capsys, "claude-code", bash(f"tail -n 5 {runs}"))[0] == 0
    assert check(monkeypatch, capsys, "claude-code", bash(f"echo x >> {runs}"))[0] == 2
    code, err = check(monkeypatch, capsys, "claude-code", bash(f"echo x > {policy_file}"), *extra)
    assert code == 2 and "protects" in err
    code, err = check(
        monkeypatch,
        capsys,
        "opencode",
        {"tool": "bash", "args": {"command": f"sed -i s/a/b/ {gate_dir}/opencode.json"}},
        *extra,
    )
    assert code == 2
    assert check(monkeypatch, capsys, "claude-code", bash(f"cat {policy_file}"), *extra)[0] == 0
    # --policy names the file that is protected, not just the default one
    other = home / "elsewhere.toml"
    write_policy(home, "")
    other.write_text("")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(other)}}
    assert check(monkeypatch, capsys, "claude-code", payload, "--policy", str(other))[0] == 2


def test_check_opencode_search_tools(home, monkeypatch, capsys):
    ssh = str(home / "home" / ".ssh")
    for tool in ("grep", "glob", "list"):
        assert check(monkeypatch, capsys, "opencode", {"tool": tool, "args": {"path": ssh, "pattern": "x"}})[0] == 2
        assert check(monkeypatch, capsys, "opencode", {"tool": tool, "args": {"path": "src", "pattern": "x"}})[0] == 0
        assert check(monkeypatch, capsys, "opencode", {"tool": tool, "args": {"pattern": "x"}})[0] == 0
    assert check(monkeypatch, capsys, "opencode", {"tool": "grep", "args": {"path": 5}})[0] == 2  # cannot read: deny


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


def test_opencode_edit_patterns_never_drop_the_parent_folder(home, tmp_path):
    out = home / "wshome" / "gate" / "opencode"
    assert generate("opencode", out) == 0
    edit = json.loads((out / "opencode.json").read_text())["permission"]["edit"]
    assert "**/wshome/gate/opencode/**" in edit and "**/wshome/logs/**" in edit and "**/wshome/policy.toml" in edit
    bare = [k for k in edit if k.startswith("**/") and "/" not in k[3:].rstrip("/*")]
    assert not bare, bare  # nothing like **/logs/** or **/opencode/**, which would hit any project's own folder


def test_the_ws_package_ships_python_files_only():
    """The secret wall copies ws as a plain snapshot of .py files and refuses anything else (the shim is a string)."""
    pkg = Path(gate.__file__).parent
    others = [p for p in pkg.rglob("*") if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts]
    assert others == []
