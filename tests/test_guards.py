"""Guards (ws.guards): the registry, the three that ship, what the gate does with their findings, `ws gate event` and
`ws gate replay`, and the hooks `ws gate generate` writes for them."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ws import adapters, approvals, cli, gate, guards, jsonl, policy, session
from ws.adapters import Call, claude_code, opencode
from ws.guards import Finding, bulk_staging, session_context, syntax_check


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def _P(name: str) -> Path:
    return Path("/x") / name


def pol(on: str = "", extra: str = "") -> policy.Policy:
    names = ", ".join(f'"{n}"' for n in on.split())
    return policy.loads(f"[guards]\non = [{names}]\n{extra}" if on or extra else "")


def write_policy(home, text: str):
    p = home / "wshome" / "policy.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def run_gate(monkeypatch, capsys, sub: str, payload, *extra) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    code = cli.main(["gate", sub, "--runner", "claude-code", *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- the registry ------------------------------------------------------------------------------------------------


def test_the_registry_is_the_folder_and_every_guard_follows_the_contract():
    found = guards.all_guards()
    assert {"bulk-staging", "syntax-check", "session-context", "record-calls"} <= set(found)
    for name, mod in found.items():
        assert mod.NAME == name
        assert mod.EVENTS and set(mod.EVENTS) <= set(guards.EVENTS)
        assert isinstance(mod.KINDS, tuple) and callable(mod.config) and callable(mod.judge)
    assert guards.names() == tuple(sorted(found))
    with pytest.raises(KeyError, match="unknown guard 'nope'"):
        guards.get("nope")


def test_a_finding_has_a_known_action_and_is_not_approvable_unless_it_says_so():
    assert Finding("deny", "x").approvable is False
    with pytest.raises(ValueError):
        Finding("block", "x")


def test_the_default_policy_text_names_every_guard():
    text = policy.default_toml()
    for name in guards.names():
        assert name in text, name
    assert policy.default_policy().guards.on == ("syntax-check", "session-context")


# --- Call.data from each Claude Code tool ------------------------------------------------------------------------


def parse(name: str, **inp):
    return claude_code.parse({"tool_name": name, "tool_input": inp})


def test_calls_carry_the_data_each_tool_gives():
    edit = parse("Edit", file_path="/a.py", old_string="x", new_string="y")
    assert (edit.kind, edit.value) == ("write", "/a.py")
    assert (edit.data["old"], edit.data["new"], edit.data["tool"]) == ("x", "y", "Edit")
    assert parse("Write", file_path="/a.py", content="c").data["content"] == "c"
    multi = parse("MultiEdit", file_path="/a.py", edits=[{"old_string": "a", "new_string": "b"}])
    assert multi.data["edits"] == ({"old": "a", "new": "b"},)
    nb = parse("NotebookEdit", notebook_path="/n.ipynb", new_source="print(1)")
    assert (nb.value, nb.data["new"]) == ("/n.ipynb", "print(1)")
    agent = parse("Agent", subagent_type="reviewer", prompt="look", description="d")
    assert (agent.kind, agent.value, agent.data["prompt"]) == ("agent", "reviewer", "look")
    assert parse("Task", prompt="p").kind == "agent"
    skill = parse("Skill", skill="align", args="a b")
    assert (skill.kind, skill.value, skill.data["name"], skill.data["args"]) == ("skill", "align", "align", "a b")
    msg = parse("SendMessage", to="worker", message="hello")
    assert (msg.kind, msg.value, msg.data["text"]) == ("message", "worker", "hello")
    assert parse("Bash", command="ls") == Call("command", "ls")


def test_call_data_is_read_only_and_does_not_break_equality_or_hashing():
    c = Call.of("write", "/a", old="x")
    with pytest.raises(TypeError):
        c.data["old"] = "y"  # type: ignore[index]
    assert Call("command", "ls") == Call("command", "ls") and hash(Call("command", "ls")) == hash(Call("command", "ls"))
    assert {c: 1}[c] == 1


def test_the_policy_judges_only_the_kinds_it_has_rules_for():
    p = pol()
    for kind in ("agent", "skill", "message"):
        assert gate.evaluate(p, Call(kind, "x")) is None
    with pytest.raises(ValueError):
        gate.evaluate(p, Call("nonsense", "x"))


# --- bulk-staging ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git add -A",
        "git add --all",
        "git add .",
        "git add ./",
        "git add :/",
        "git add -f .",
        "git add -Av",
        "git -C x add -A",
        "git -c user.name=x -C /y add .",
        "git commit -a",
        "git commit -am m",
        "git commit --all -m m",
        "git commit -m m -a",
        "git -C x commit -am m",
        "ls && git add -A",
        "echo hi | git add .",
        "sudo git add -A",
        "bash -c 'git add -A'",
        "echo $(git add -A)",
        "git add -- .",
        "git add a.py .",
        "git stage -A",
        "git stage .",
        "git stage --all",
        "git -C x stage :/",
        "git add --a",
        "git add --al",
        "git stage --al",
        "git commit --al -m m",
        "git add :(top)",
        "git add ':(top)'",
        "git add ':(top).'",
        "git add ':(top,glob)*'",
        "git add :/.",
        "git add '*'",
        "git -c alias.go='add -A' go",
        "git -calias.go='add -A' go",
        "git -C x -c alias.go=status go",
        "git --config-env=alias.go=GOV go",
        "git --config-env alias.go=GOV go",
        'eval "git add -A"',
        "eval git add .",
        "FOO=1 eval 'ls; git commit -am m'",
        "bash -lc 'git stage .'",
        'echo "<<X"\ngit add -A',
    ],
)
def test_bulk_staging_stops_whole_tree_staging(cmd):
    found = bulk_staging.judge(Call("command", cmd), None, None)
    assert found is not None and found.action == "deny" and not found.approvable, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git add a.py b.py",
        "git add -- a.py",
        "git add src/",
        "git add -u src/",  # -u with a pathspec of its own stays; with none it is the whole tree (below)
        "git add -u -- a.py",
        "git -C x add file.txt",
        'git add "$f" other.py',
        "git add $PWD/a.py",
        "git commit -m . a.py",
        "git commit -m m -- a.py",
        "git commit -i a.py -m m",
        "git commit -m m -o src/",
        "echo $'it\\'s'\ngit status",
        "ls #\\\ngit status",
        "echo ${x#<<X}\ngit status",
        "git -c user.name=x -c Core.editor=true commit -m m",
        "GIT_AUTHOR_NAME=x git commit -m m",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.editor GIT_CONFIG_VALUE_0=true git status",
        "git commit -m 'fix it'",
        'git commit -m "git add -A"',
        'git commit -m "use -a here"',
        "git commit -m -a",
        "git commit --amend --no-edit",
        "git commit -ma",
        "git status",
        "git stage a.py",
        "git add ':(top)src/a.py'",
        "git add :/src/a.py",
        "git add ':(glob)*.py'",
        "git commit --amend",
        "git -c user.name=x -c core.editor=true commit -m m",
        "echo git add -A",
        "eval 'echo git add -A'",
        "ls # git add -A",
        "git log --all",
        "git commit -m \"$(cat <<'EOF'\ngit add -A\nand git commit -a\nEOF\n)\"",
        "cat <<EOF > notes.txt\ngit add .\nEOF",
        "cat <<-'EOF'\n\tgit add -A\n\tEOF\nls",
    ],
)
def test_bulk_staging_lets_named_paths_and_quoted_text_through(cmd):
    assert bulk_staging.judge(Call("command", cmd), None, None) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # quoting the scan once misread, each hiding the line below
        "echo $'it\\'s'\ngit add -A",
        "echo $'a\\'b'; git add -A",
        "echo $(echo $'it\\'s')\ngit add -A",
        "ls #\\\ngit add -A",
        "ls # note \\\\\ngit add -A",
        "echo ${x#<<X}\ngit add -A",
        "echo ${x/<<X/}\ngit add -A",
        "echo ${x:-<<X}\ngit add -A",
        "echo $[x<<X]\ngit add -A",
        "echo ${x:-${y#<<X}}\ngit add -A",
        "$'g\\x69t' add -A",
        "git add $'-\\x41'",
        # git config keys are not case-sensitive
        "git -c Alias.go='add -A' go",
        "git -c ALIAS.go='add -A' go",
        "git -cAlias.go='add -A' go",
        "git --config-env=ALIAS.go=GOV go",
        "git --config-env Alias.go=GOV go",
        # the same through the environment
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.go GIT_CONFIG_VALUE_0='add -A' git go",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=Alias.go GIT_CONFIG_VALUE_0='add -A' git go",
        "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=ALIAS.go GIT_CONFIG_VALUE_0='add -A' git go",
        "GIT_CONFIG_PARAMETERS=\"'alias.go=add -A'\" git go",
        "GIT_CONFIG_PARAMETERS= git status",
        "export GIT_CONFIG_PARAMETERS=\"'alias.go=add -A'\"; git go",
        "export GIT_CONFIG_KEY_0=alias.go; git go",
        "GIT_CONFIG_KEY_0=$K git go",
        # more ways to name the whole tree
        "git add ./.",
        "git add .//",
        "git add ./",
        "git add ..",
        "git add '**'",
        'git add "$PWD"',
        "git add $PWD",
        "git add ${PWD}",
        "git add $(pwd)",
        'git add "$(pwd)"',
        "git add `pwd`",
        "git stage $(pwd)",
        "git add -u",
        "git add --update",
        "git add --upd",
        "git add -uv",
        "git add -u -v",
        "git stage -u",
        "git commit .",
        "git commit -- .",
        "git commit -i .",
        "git commit --include .",
        "git commit -o .",
        "git commit --only .",
        "git commit -m m .",
        "git commit -m m -- .",
        "git commit -m m -i ./.",
        "git commit -m m :/",
        "git commit -m m $PWD",
        "git -C x commit -m m .",
    ],
)
def test_bulk_staging_stops_each_bypass_the_reviewer_found(cmd):
    found = bulk_staging.judge(Call("command", cmd), None, None)
    assert found is not None and found.action == "deny" and not found.approvable, cmd


def test_the_gate_denies_a_quote_left_open_with_a_clear_reason(home):
    reason = gate.check("claude-code", json.dumps(bash("echo 'it\ngit status")), pol("bulk-staging"))
    assert reason and "unclosed" in reason
    assert gate.check("claude-code", json.dumps(bash("echo it\\'s")), pol("bulk-staging")) is None


def test_bulk_staging_reads_a_command_after_a_heredoc_ends():
    cmd = "cat <<EOF\nhello\nEOF\ngit add -A"
    assert bulk_staging.judge(Call("command", cmd), None, None) is not None


def test_bulk_staging_takes_no_table():
    with pytest.raises(policy.PolicyError, match=r"\[guards.bulk-staging\]: unknown key 'x'"):
        pol("bulk-staging", "[guards.bulk-staging]\nx = 1\n")


def test_bulk_staging_adds_native_deny_patterns_only_while_on():
    assert "git add -A*" not in policy.native_patterns(pol())
    on = policy.native_patterns(pol("bulk-staging"))
    assert {"git add -A*", "git add .", "git commit -a*", "git -* add -A*"} <= set(on)
    assert {"git stage -A*", "git stage .", "git add --a*", "git -c alias.*", "git * -c alias.*"} <= set(on)
    deny = json.loads(claude_code.generate(pol("bulk-staging"), ["ws"], _P("o"), _P("p.toml"))["settings.json"])
    assert "Bash(git add -A*)" in deny["permissions"]["deny"]


def test_the_gate_denies_bulk_staging_and_allows_named_files(home):
    p = pol("bulk-staging")
    reason = gate.check("claude-code", json.dumps(bash("git -C x add -A")), p)
    assert reason and reason.startswith("bulk-staging: ")
    assert gate.check("claude-code", json.dumps(bash("git add a.py")), p) is None
    assert gate.check("claude-code", json.dumps(bash("git add -A")), pol()) is None  # not on: no guard


# --- syntax-check ------------------------------------------------------------------------------------------------


def judge_file(path, cwd="", **table):
    cfg = syntax_check.config(table, "[guards.syntax-check]")
    ctx = guards.Context("claude-code", "post", cwd=cwd)
    return syntax_check.judge(Call("write", str(path)), cfg, ctx)


def test_syntax_check_names_the_line_of_a_python_error(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ndef f(:\n    pass\n")
    found = judge_file(f)
    assert found.action == "deny" and "line 2" in found.reason and str(f) in found.reason
    f.write_text("x = 1\n")
    assert judge_file(f) is None


def test_syntax_check_reads_json_and_toml(tmp_path):
    j, t = tmp_path / "a.json", tmp_path / "a.toml"
    j.write_text('{"a": 1,}')
    t.write_text("a = [\n")
    assert "not valid JSON" in judge_file(j).reason and "line 1" in judge_file(j).reason
    assert "not valid TOML" in judge_file(t).reason
    j.write_text('{"a": 1}')
    t.write_text("a = 1\n")
    assert judge_file(j) is None and judge_file(t) is None


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a shell")
def test_syntax_check_runs_sh_n_on_shell_scripts(tmp_path):
    f = tmp_path / "a.sh"
    f.write_text("if true; then\necho hi\n")
    found = judge_file(f)
    assert found.action == "deny" and "sh found a problem" in found.reason
    f.write_text("echo hi\n")
    assert judge_file(f) is None


def test_syntax_check_says_so_when_there_is_no_shell(tmp_path, monkeypatch):
    f = tmp_path / "a.sh"
    f.write_text("echo hi\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    found = judge_file(f)
    assert found.action == "note" and "skipped" in found.reason


def test_syntax_check_runs_the_checker_the_policy_names_for_an_extension(tmp_path):
    checker = [sys.executable, "-c", "import sys; sys.exit(1 if 'bad' in open(sys.argv[1]).read() else 0)"]
    f = tmp_path / "a.yaml"
    f.write_text("bad: [\n")
    found = judge_file(f, extra={"yaml": checker})
    assert found.action == "deny" and "found a problem" in found.reason
    f.write_text("ok: 1\n")
    assert judge_file(f, extra={"yaml": checker}) is None
    assert judge_file(f) is None  # an extension with no checker is left alone


def test_syntax_check_notes_a_missing_checker_and_a_slow_one(tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text("x\n")
    assert "not installed" in judge_file(f, extra={"yaml": ["no-such-checker-ws"]}).reason
    monkeypatch.setattr(syntax_check, "TIMEOUT_S", 0.2)
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    assert "took more than" in judge_file(f, extra={"yaml": slow}).reason


def test_syntax_check_resolves_a_relative_path_against_the_session_folder(tmp_path):
    (tmp_path / "a.py").write_text("def f(:\n")
    assert judge_file("a.py", cwd=str(tmp_path)).action == "deny"
    assert judge_file("missing.py", cwd=str(tmp_path)) is None


@pytest.mark.parametrize(
    "extra, message",
    [
        ('extra = { "Y.aml" = ["x"] }', "is not an extension"),
        ('extra = { yaml = "yamllint" }', "extra.yaml must be a list"),
        ("extra = { yaml = [] }", "extra.yaml must be a list"),
        ("extra = 5", "extra must be a table"),
        ("other = 1", "unknown key 'other'"),
    ],
)
def test_syntax_check_rejects_a_bad_table(extra, message):
    with pytest.raises(policy.PolicyError, match=message):
        pol("syntax-check", f"[guards.syntax-check]\n{extra}\n")


# --- session-context ---------------------------------------------------------------------------------------------


def test_session_context_lists_the_state_files_that_exist(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".claude-state").mkdir()
    (tmp_path / ".claude-state" / "STATE.md").write_text("state")
    (tmp_path / "sub").mkdir()
    cfg = session_context.config({"files": [".claude-state/STATE.md", "NOPE.md"]}, "[guards.session-context]")
    ctx = guards.Context("claude-code", "start", cwd=str(tmp_path / "sub"))  # from below the git top level
    found = session_context.judge(None, cfg, ctx)
    assert found.action == "note"
    assert found.reason == f"ws: .claude-state/STATE.md exists at {tmp_path / '.claude-state' / 'STATE.md'}"
    assert session_context.judge(None, cfg, guards.Context("claude-code", "start")) is None
    empty = guards.Context("claude-code", "start", cwd=str(tmp_path / "sub"))
    assert session_context.judge(None, session_context.Config(("NOPE.md",)), empty) is None


def test_session_context_looks_in_the_main_worktree_too(tmp_path):
    main, linked = tmp_path / "main", tmp_path / "linked"
    (main / ".claude-state").mkdir(parents=True)
    (main / ".claude-state" / "STATE.md").write_text("state")
    gitdir = main / ".git" / "worktrees" / "w"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")
    found = session_context.judge(
        None, session_context.Config(), guards.Context("claude-code", "start", cwd=str(linked))
    )
    assert found.reason.endswith("(main worktree)") and str(main) in found.reason


def test_session_context_default_files_and_bad_paths():
    assert session_context.config({}, "[w]").files == (".claude-state/STATE.md",)
    for bad in ("/etc/passwd", "../x", "~/x", "."):
        with pytest.raises(policy.PolicyError, match="inside the project"):
            session_context.config({"files": [bad]}, "[w]")


# --- record-calls ------------------------------------------------------------------------------------------------


def test_record_calls_appends_one_line_per_hook_run_and_never_denies(home):
    p = pol("record-calls")
    glob = {"tool_name": "Glob", "tool_input": {"pattern": "/etc/*", "path": "/tmp"}, "session_id": "s"}  # two calls
    assert gate.check("claude-code", json.dumps(glob), p, clock=lambda: 0) is None
    lines = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    assert len(lines) == 1
    assert lines[0] == {"at": "1970-01-01T00:00:00+00:00", "runner": "claude-code", "payload": glob}


SECRET_TEXT = "planted-" + "7" * 12  # built at run time


def _fingerprint(text: str) -> dict:
    return {"bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}


@pytest.mark.parametrize(
    "tool, inp, redacted",
    [
        ("Write", {"file_path": "/p/a.txt", "content": SECRET_TEXT}, {"content": SECRET_TEXT}),
        (
            "Edit",
            {"file_path": "/p/a.txt", "old_string": "before " + SECRET_TEXT, "new_string": "after " + SECRET_TEXT},
            {"old_string": "before " + SECRET_TEXT, "new_string": "after " + SECRET_TEXT},
        ),
        (
            "MultiEdit",
            {"file_path": "/p/a.txt", "edits": [{"old_string": "a" + SECRET_TEXT, "new_string": "b" + SECRET_TEXT}]},
            {},
        ),
        (
            "NotebookEdit",
            {"notebook_path": "/p/n.ipynb", "new_source": SECRET_TEXT, "cell_id": "c1"},
            {"new_source": SECRET_TEXT},
        ),
    ],
)
def test_record_calls_keeps_the_path_and_a_fingerprint_not_the_text_of_a_write(home, tool, inp, redacted):
    payload = {"tool_name": tool, "tool_input": inp, "session_id": "s"}
    assert gate.check("claude-code", json.dumps(payload), pol("record-calls")) is None
    raw = (home / "wshome" / "logs" / "gate-calls.jsonl").read_text()
    assert SECRET_TEXT not in raw
    (line,) = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    got = line["payload"]["tool_input"]
    assert got.get("file_path", got.get("notebook_path")) == inp.get("file_path", inp.get("notebook_path"))
    for key, text in redacted.items():
        assert got[key] == _fingerprint(text)
    if tool == "MultiEdit":
        assert got["edits"] == [
            {"old_string": _fingerprint("a" + SECRET_TEXT), "new_string": _fingerprint("b" + SECRET_TEXT)}
        ]
    assert got.get("cell_id", "c1") == "c1"
    assert line["payload"]["session_id"] == "s"


def test_record_calls_redacts_open_code_writes_and_leaves_commands_and_reads_alone(home):
    write = {"tool": "write", "args": {"filePath": "/p/a", "content": SECRET_TEXT}}
    edit = {"tool": "multiedit", "args": {"filePath": "/p/a", "edits": [{"oldString": "x", "newString": SECRET_TEXT}]}}
    for payload in (write, edit):
        gate.check("opencode", json.dumps(payload), pol("record-calls"))
    gate.check("claude-code", json.dumps(bash("echo " + "hi")), pol("record-calls"))
    read = {"tool_name": "Read", "tool_input": {"file_path": "/p/a"}}
    gate.check("claude-code", json.dumps(read), pol("record-calls"))
    lines = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    assert SECRET_TEXT not in json.dumps(lines)
    assert lines[0]["payload"]["args"]["content"] == _fingerprint(SECRET_TEXT)
    assert lines[1]["payload"]["args"]["edits"][0]["newString"] == _fingerprint(SECRET_TEXT)
    assert lines[2]["payload"]["tool_input"]["command"] == "echo hi" and lines[3]["payload"] == read


@pytest.mark.parametrize(
    "tool, inp, where",
    [
        ("Write", {"file_path": "/p/a", "content": [SECRET_TEXT]}, ("content",)),  # not text: a list
        ("Write", {"file_path": "/p/a", "content": {"x": SECRET_TEXT}}, ("content",)),
        ("Edit", {"file_path": "/p/a", "old_string": "x", "new_string": [SECRET_TEXT]}, ("new_string",)),
        ("MultiEdit", {"file_path": "/p/a", "edits": [{"old_string": 1, "new_string": [SECRET_TEXT]}]}, ()),
        ("MultiEdit", {"file_path": "/p/a", "edits": [SECRET_TEXT]}, ()),  # an edit that is not a dict
        ("MultiEdit", {"file_path": "/p/a", "edits": SECRET_TEXT}, ("edits",)),  # edits that is not a list
        ("Write", {"file_path": "/p/a", "content": 12345}, ("content",)),
    ],
)
def test_record_calls_fingerprints_a_value_under_a_text_key_whatever_its_type(home, tool, inp, where):
    payload = {"tool_name": tool, "tool_input": inp}
    assert gate.check("claude-code", json.dumps(payload), pol("record-calls")) is None
    raw = (home / "wshome" / "logs" / "gate-calls.jsonl").read_text()
    assert SECRET_TEXT not in raw
    (line,) = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    got = line["payload"]["tool_input"]
    for key in where:
        assert set(got[key]) == {"bytes", "sha256"}
    if where == ("content",) and inp["content"] == 12345:
        assert got["content"] == _fingerprint("12345")


@pytest.mark.parametrize("tool", ["patch", "apply_patch"])
def test_record_calls_keeps_the_paths_and_a_fingerprint_of_an_open_code_patch(home, tool):
    text = f"*** Begin Patch\n*** Update File: /p/a.py\n@@\n-x\n+{SECRET_TEXT}\n*** End Patch"
    payload = {"tool": tool, "args": {"patchText": text}, "session_id": "s"}
    assert gate.check("opencode", json.dumps(payload), pol("record-calls")) is None
    raw = (home / "wshome" / "logs" / "gate-calls.jsonl").read_text()
    assert SECRET_TEXT not in raw
    (line,) = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    assert line["payload"]["args"]["patchText"] == _fingerprint(text)
    assert line["payload"]["paths"] == ["/p/a.py"]
    # the kept line replays by its paths: a protected file in it is still a deny, a plain one still an allow
    p = pol("record-calls")
    assert gate.check("opencode", json.dumps(line["payload"]), p) is None
    protected = str(home / "wshome" / "policy.toml")
    kept = {**line["payload"], "paths": [protected]}
    assert "protected" in gate.check("opencode", json.dumps(kept), p, (protected,))
    forged = {"tool": tool, "args": {"patchText": {"bytes": 1}}}  # a fingerprint with no paths beside it: not judged
    with pytest.raises(TypeError):
        gate.check("opencode", json.dumps(forged), p)


def test_a_recorded_write_still_replays_by_its_path(home, capsys):
    p = write_policy(home, '[guards]\non = ["record-calls"]\n')
    for tool, inp in (
        ("Write", {"file_path": "/p/a", "content": SECRET_TEXT}),
        ("MultiEdit", {"file_path": "/p/a", "edits": [{"old_string": "x", "new_string": SECRET_TEXT}]}),
    ):
        gate.check("claude-code", json.dumps({"tool_name": tool, "tool_input": inp}), policy.load(p))
    log = home / "wshome" / "logs" / "gate-calls.jsonl"
    assert cli.main(["gate", "replay", str(log), "--runner", "claude-code", "--policy", str(p)]) == 0
    assert "2 allow" in capsys.readouterr().out


def test_record_calls_bounds_what_it_writes(home):
    long = "x" * 40000
    gate.check("claude-code", json.dumps(bash("echo " + long)), pol("record-calls"))
    (line,) = jsonl.read(home / "wshome" / "logs" / "gate-calls.jsonl")
    assert "[truncated: " in line["payload"]["tool_input"]["command"]
    assert len(json.dumps(line)) < 20000


def test_record_calls_warns_instead_of_denying_when_the_log_cannot_be_written(home):
    (home / "wshome").mkdir()
    (home / "wshome" / "logs").write_text("a file where the folder should be")
    notes: list[str] = []
    assert gate.check("claude-code", json.dumps(bash("ls")), pol("record-calls"), notes=notes) is None
    assert len(notes) == 1 and notes[0].startswith("record-calls: record-calls could not write")


def test_a_recorded_git_add_replays_as_deny(home, monkeypatch, capsys):
    p = write_policy(home, '[guards]\non = ["bulk-staging", "record-calls"]\n')
    assert run_gate(monkeypatch, capsys, "check", bash("git add -A"), "--policy", str(p))[0] == 2
    assert run_gate(monkeypatch, capsys, "check", bash("git add a.py"), "--policy", str(p))[0] == 0
    log = home / "wshome" / "logs" / "gate-calls.jsonl"
    assert len(jsonl.read(log)) == 2  # record-calls is last in the list and sees only what bulk-staging let through
    assert cli.main(["gate", "replay", str(log), "--runner", "claude-code", "--policy", str(p)]) == 0
    out = capsys.readouterr().out
    assert "allow" in out and "1 allow" in out


# --- Context.state -----------------------------------------------------------------------------------------------


def test_state_is_a_locked_read_modify_write_file_per_session_and_name(home):
    ctx = guards.Context("claude-code", "pre", session_id="s1", home=home / "wshome")
    assert ctx.state("count") == {}

    def bump(d):
        d["n"] = d.get("n", 0) + 1

    threads = [threading.Thread(target=lambda: [ctx.state("count", bump) for _ in range(10)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ctx.state("count") == {"n": 80}
    path = home / "wshome" / "sessions" / "s1.count.json"
    assert json.loads(path.read_text()) == {"n": 80}
    assert (path.stat().st_mode & 0o777) == 0o600
    other = guards.Context("claude-code", "pre", session_id="s2", home=home / "wshome")
    assert other.state("count") == {} and ctx.state("other") == {}
    assert ctx.state("count", lambda d: {"fresh": True}) == {"fresh": True}  # returning a dict replaces it


def test_state_starts_again_from_a_record_it_cannot_read_and_refuses_odd_names(home):
    ctx = guards.Context("claude-code", "pre", session_id="s1", home=home / "wshome")
    ctx.state("a", lambda d: d.update(x=1))
    (home / "wshome" / "sessions" / "s1.a.json").write_text("{not json")
    assert ctx.state("a") == {}
    for bad in ("", "../x", "a.b", "a/b"):
        with pytest.raises(ValueError):
            ctx.state(bad)
    with pytest.raises(ValueError):
        guards.Context("claude-code", "pre", home=home / "wshome").state("a")


def test_a_session_id_that_is_not_a_plain_name_is_hashed_for_its_state_file(home):
    ctx = guards.Context("claude-code", "pre", session_id="../../evil", home=home / "wshome")
    ctx.state("a", lambda d: d.update(x=1))
    assert [p.name for p in (home / "wshome").rglob("*.a.json")] == [f"{session.safe_id('../../evil')}.a.json"]


def test_state_files_are_pruned_with_the_session_records(home):
    import os
    import time

    ctx = guards.Context("claude-code", "pre", session_id="old", home=home / "wshome")
    ctx.state("a", lambda d: d.update(x=1))
    path = home / "wshome" / "sessions" / "old.a.json"
    long_ago = time.time() - 40 * 86400
    os.utime(path, (long_ago, long_ago))
    session._prune(path.parent)
    assert not path.exists()


# --- the gate and an approvable deny -----------------------------------------------------------------------------


def fake_guard(monkeypatch, name="fake", events=("pre",), kinds=("command",), finding=None, judge=None, **extra):
    mod = SimpleNamespace(
        NAME=name,
        EVENTS=events,
        KINDS=kinds,
        config=lambda table, where: guards.known_keys(table, where),
        judge=judge or (lambda call, cfg, ctx: finding),
        **extra,
    )
    real = guards.all_guards
    monkeypatch.setattr(guards, "all_guards", lambda: {**real(), name: mod})
    return mod


def test_an_approvable_deny_asks_a_person_once_like_ask_commands(home, monkeypatch):
    fake_guard(monkeypatch, finding=Finding("deny", "this looks risky", approvable=True))
    p = pol("fake")
    root = home / "approvals"
    raw = json.dumps({**bash("make deploy"), "session_id": "s1", "cwd": "/work"})
    asked = gate.check("claude-code", raw, p, approval_root=root)
    assert isinstance(asked, str) and "fake: this looks risky: needs a person's approval" in asked
    ((ident, bound, _),) = approvals.pending(root)
    assert bound == approvals.Bound("claude-code", "guard:fake", "make deploy", "/work", "s1")
    approvals.grant(root, ident[:12])
    assert gate.check("claude-code", raw, p, approval_root=root) is None  # the retry goes through
    assert gate.check("claude-code", raw, p, approval_root=root)  # and that was the one use
    other = json.dumps({**bash("make deploy2"), "session_id": "s1", "cwd": "/work"})
    assert "needs a person's approval" in gate.check("claude-code", other, p, approval_root=root)


def test_an_approvable_deny_with_no_approvals_folder_is_an_ask(home, monkeypatch):
    fake_guard(monkeypatch, finding=Finding("deny", "risky", approvable=True))
    reason = gate.check("claude-code", json.dumps(bash("ls")), pol("fake"))
    assert isinstance(reason, gate.Ask) and "fake: risky" in reason


def test_a_deny_that_is_not_approvable_wins_in_policy_order_and_warnings_are_noted(home, monkeypatch):
    fake_guard(monkeypatch, "a-warn", finding=Finding("warn", "careful"))
    fake_guard(monkeypatch, "b-note", finding=Finding("note", "fyi"))
    fake_guard(monkeypatch, "c-deny", finding=Finding("deny", "no"))
    fake_guard(monkeypatch, "d-deny", finding=Finding("deny", "also no"))
    notes: list[str] = []
    reason = gate.check("claude-code", json.dumps(bash("ls")), pol("a-warn b-note c-deny d-deny"), notes=notes)
    assert reason == "c-deny: no" and not isinstance(reason, gate.Ask)
    assert notes == ["a-warn: careful", "b-note: fyi"]


def test_check_prints_a_warning_on_stderr_and_still_allows(home, monkeypatch, capsys):
    fake_guard(monkeypatch, finding=Finding("warn", "mind the gap"))
    p = write_policy(home, '[guards]\non = ["fake"]\n')
    code, _, err = run_gate(monkeypatch, capsys, "check", bash("ls"), "--policy", str(p))
    assert code == 0 and "ws gate: fake: mind the gap" in err


def test_a_guard_only_sees_the_kinds_it_reads(home, monkeypatch):
    seen = []
    fake_guard(monkeypatch, kinds=("skill",), judge=lambda call, cfg, ctx: seen.append(call.kind))
    gate.check("claude-code", json.dumps(bash("ls")), pol("fake"))
    gate.check("claude-code", json.dumps({"tool_name": "Skill", "tool_input": {"skill": "align"}}), pol("fake"))
    assert seen == ["skill"]


def test_a_guard_that_crashes_before_a_call_is_a_deny(home, monkeypatch, capsys):
    def boom(call, cfg, ctx):
        raise RuntimeError("guard bug")

    fake_guard(monkeypatch, judge=boom)
    p = write_policy(home, '[guards]\non = ["fake"]\n')
    code, _, err = run_gate(monkeypatch, capsys, "check", bash("ls"), "--policy", str(p))
    assert code == 2 and "blocked to be safe" in err


# --- policy: the [guards] table ----------------------------------------------------------------------------------


def test_no_guards_table_means_no_guards():
    p = policy.loads("")
    assert p.guards.on == () and p.guards.config == {}
    assert gate.check("claude-code", json.dumps(bash("git add -A")), p) is None


@pytest.mark.parametrize(
    "text, message",
    [
        ('[guards]\non = ["nope"]\n', "unknown guard 'nope'"),
        ('[guards]\non = ["syntax-check", "syntax-check"]\n', "named twice"),
        ("[guards]\non = [1]\n", "on must be a list of non-empty text"),
        ('[guards]\non = "syntax-check"\n', "on must be a list"),
        ("[guards]\nturbo = true\n", "unknown key 'turbo'"),
        ("guards = 5\n", "must be a table"),
        ("[guards.session-context]\nfiles = 5\n", "files must be a list"),
        ("[guards.session-context]\nother = 5\n", "unknown key 'other'"),
        ("[guards.nope]\nx = 1\n", "unknown key 'nope'"),
    ],
)
def test_the_guards_table_is_checked_and_names_the_key(text, message):
    with pytest.raises(policy.PolicyError, match=message):
        policy.loads(text)


def test_each_guards_table_is_read_into_its_config():
    p = pol(
        "syntax-check session-context",
        '[guards.syntax-check]\nextra = { yaml = ["yamllint", "-d", "relaxed"] }\n'
        '[guards.session-context]\nfiles = ["NOTES.md"]\n',
    )
    assert p.guards.config["syntax-check"].extra == {"yaml": ("yamllint", "-d", "relaxed")}
    assert p.guards.config["session-context"].files == ("NOTES.md",)


# --- ws gate event -----------------------------------------------------------------------------------------------


def post(tool="Edit", path="/x", **inp):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": path, **inp}}


def test_event_after_an_edit_with_a_python_error_exits_2_with_the_line(home, monkeypatch, capsys):
    p = write_policy(home, '[guards]\non = ["syntax-check"]\n')
    f = home / "bad.py"
    f.write_text("a = 1\nb = (\n")
    code, out, err = run_gate(monkeypatch, capsys, "event", post(path=str(f)), "--policy", str(p))
    assert code == 2 and out == "" and "syntax-check: " in err and "line 2" in err
    f.write_text("a = 1\n")
    assert run_gate(monkeypatch, capsys, "event", post(path=str(f)), "--policy", str(p)) == (0, "", "")


def test_event_after_a_note_exits_0_and_a_tool_no_guard_reads_is_ignored(home, monkeypatch, capsys):
    f = home / "a.yaml"
    f.write_text("x\n")
    p = write_policy(
        home, '[guards]\non = ["syntax-check"]\n[guards.syntax-check]\nextra = { yaml = ["no-such-checker-ws"] }\n'
    )
    code, out, err = run_gate(monkeypatch, capsys, "event", post(path=str(f)), "--policy", str(p))
    assert code == 0 and out == "" and "not installed" in err
    bashed = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert run_gate(monkeypatch, capsys, "event", bashed, "--policy", str(p)) == (0, "", "")


def test_event_at_session_start_puts_the_notes_on_stdout(home, monkeypatch, capsys):
    p = write_policy(home, '[guards]\non = ["session-context"]\n')
    project = home / "proj"
    (project / ".git").mkdir(parents=True)
    (project / ".claude-state").mkdir()
    (project / ".claude-state" / "STATE.md").write_text("s")
    payload = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(project)}
    code, out, err = run_gate(monkeypatch, capsys, "event", payload, "--policy", str(p))
    assert code == 0 and err == "" and ".claude-state/STATE.md exists at" in out and "session-context" in out


@pytest.mark.parametrize(
    "name, ev", [("Stop", "stop"), ("SubagentStop", "subagent-stop"), ("UserPromptSubmit", "prompt")]
)
def test_events_that_only_log_exit_0_whatever_the_finding(home, monkeypatch, capsys, name, ev):
    fake_guard(monkeypatch, events=(ev,), kinds=(), finding=Finding("deny", "stop doing that"))
    p = write_policy(home, '[guards]\non = ["fake"]\n')
    code, out, err = run_gate(monkeypatch, capsys, "event", {"hook_event_name": name}, "--policy", str(p))
    assert code == 0
    assert "fake: stop doing that" in (out if ev == "prompt" else err)


def test_event_never_blocks_on_a_crash_a_bad_payload_or_a_broken_policy(home, monkeypatch, capsys):
    def boom(call, cfg, ctx):
        raise RuntimeError("guard bug")

    fake_guard(monkeypatch, events=("post",), kinds=("write",), judge=boom)
    p = write_policy(home, '[guards]\non = ["fake"]\n')
    for payload in (post(), "not json", "[]", {"hook_event_name": "PostToolUse", "tool_name": "Edit"}):
        code, out, err = run_gate(monkeypatch, capsys, "event", payload, "--policy", str(p))
        assert code == 0 and out == "" and err.startswith("ws gate event: skipped (")
    write_policy(home, "dial = [\n")
    code, _, err = run_gate(monkeypatch, capsys, "event", post(), "--policy", str(p))
    assert code == 0 and "skipped" in err


def test_event_for_a_name_ws_does_not_answer_is_a_quiet_exit_0(home, monkeypatch, capsys):
    p = write_policy(home, '[guards]\non = ["syntax-check"]\n')
    for name in ("PreToolUse", "Notification", "Nonsense", None):
        code, out, err = run_gate(monkeypatch, capsys, "event", {"hook_event_name": name}, "--policy", str(p))
        assert code == 0 and out == "" and "is answered by `ws gate check` or is not a hook event" in err


def test_a_runner_with_no_event_names_exits_0(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    assert cli.main(["gate", "event", "--runner", "opencode"]) == 0
    assert "is not a hook event ws reads" in capsys.readouterr().err


# --- ws gate replay ----------------------------------------------------------------------------------------------


def test_replay_prints_allow_deny_ask_and_exits_1_on_a_mismatch(home, monkeypatch, capsys):
    p = write_policy(home, '[gate]\nask_commands = ["terraform apply*"]\n[guards]\non = ["bulk-staging"]\n')
    lines = [
        json.dumps(bash("ls")),
        json.dumps({"payload": bash("git add -A"), "expect": "deny"}),
        json.dumps({"at": "x", "runner": "claude-code", "payload": bash("terraform apply")}),
        "",
        json.dumps({"payload": bash("ls -la"), "expect": "deny"}),
    ]
    f = home / "calls.jsonl"
    f.write_text("\n".join(lines) + "\n")
    assert cli.main(["gate", "replay", str(f), "--runner", "claude-code", "--policy", str(p)]) == 1
    out = capsys.readouterr().out
    rows = [ln.split() for ln in out.splitlines()]
    assert [r[1] for r in rows[:4]] == ["allow", "deny", "ask", "allow"]
    assert "(expected deny)" in out and "1 differ from expect" in out
    assert "4 calls: 2 allow, 1 deny, 1 ask" in out
    assert not (home / "wshome" / "approvals").exists()  # an ask is reported, never written


def test_replay_exits_0_when_every_expect_holds_and_reports_json(home, monkeypatch, capsys):
    p = write_policy(home, '[guards]\non = ["bulk-staging"]\n')
    f = home / "calls.jsonl"
    f.write_text(json.dumps({"payload": bash("git add ."), "expect": "deny"}) + "\n" + json.dumps(bash("ls")) + "\n")
    assert cli.main(["--json", "gate", "replay", str(f), "--runner", "claude-code", "--policy", str(p)]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [r["result"] for r in data["results"]] == ["deny", "allow"] and data["differ"] == 0
    assert data["results"][0]["reason"].startswith("bulk-staging: ")


def test_replay_judges_an_unreadable_payload_as_a_deny_and_refuses_a_bad_file(home, monkeypatch, capsys):
    p = write_policy(home, "")
    f = home / "calls.jsonl"
    f.write_text(json.dumps({"tool_name": "Bash"}) + "\n")
    assert cli.main(["gate", "replay", str(f), "--runner", "claude-code", "--policy", str(p)]) == 0
    assert "deny" in capsys.readouterr().out
    for text in ("not json\n", "[1]\n", json.dumps({"payload": bash("ls"), "expect": "maybe"}) + "\n"):
        f.write_text(text)
        assert cli.main(["gate", "replay", str(f), "--runner", "claude-code", "--policy", str(p)]) == 1
        assert "line 1" in capsys.readouterr().err
    assert cli.main(["gate", "replay", str(home / "missing.jsonl"), "--runner", "claude-code"]) == 1


def test_replay_keeps_no_guard_state(home, monkeypatch, capsys):
    def touch(call, cfg, ctx):
        ctx.state("seen", lambda d: d.update(n=1))

    fake_guard(monkeypatch, judge=touch)
    p = write_policy(home, '[guards]\non = ["fake"]\n')
    f = home / "calls.jsonl"
    f.write_text(json.dumps({**bash("ls"), "session_id": "s"}) + "\n")
    assert cli.main(["gate", "replay", str(f), "--runner", "claude-code", "--policy", str(p)]) == 0
    assert not (home / "wshome" / "sessions").exists()


# --- ws gate generate --------------------------------------------------------------------------------------------


def generate(p: policy.Policy, **kw) -> dict:
    return json.loads(claude_code.generate(p, ["ws"], _P("out"), _P("wshome/policy.toml"), **kw)["settings.json"])


def commands(group: list[dict]) -> list[str]:
    return [h["command"] for g in group for h in g["hooks"]]


def test_with_no_guards_the_hooks_are_the_old_ones():
    hooks = generate(pol())["hooks"]
    assert set(hooks) == {
        "PreToolUse",
        "SessionStart",
        "UserPromptSubmit",
        "Notification",
        "Stop",
    }  # the last two: phone alerts
    assert hooks["PreToolUse"][0]["matcher"] == claude_code.MATCHER
    assert commands(hooks["SessionStart"]) == ["ws session start --runner claude-code || true"]
    assert generate(pol())["env"] == {"WS_GATED_SESSION": "1", "WS_SANDBOXED": "1"}


def test_the_default_policy_adds_post_tool_use_and_session_start_events():
    hooks = generate(policy.default_policy())["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    event = "ws gate event --runner claude-code --policy /x/wshome/policy.toml"
    assert commands(hooks["PostToolUse"]) == [event]  # no `|| exit`: exit 2 here is a message to the model
    assert commands(hooks["SessionStart"]) == ["ws session start --runner claude-code || true", event + " || true"]
    assert hooks["PostToolUse"][0]["hooks"][0]["timeout"] > 10  # the checkers get 10 seconds
    assert "SubagentStop" not in hooks
    alert = "ws notify alert --runner claude-code || true"
    assert commands(hooks["Stop"]) == [alert] and commands(hooks["Notification"]) == [alert]


def test_each_event_gets_a_hook_only_when_a_guard_runs_there(monkeypatch):
    fake_guard(monkeypatch, "e-stop", events=("stop", "subagent-stop", "prompt"), kinds=())
    fake_guard(monkeypatch, "e-post", events=("post",), kinds=("agent", "skill", "command"))
    fake_guard(monkeypatch, "e-pre", events=("pre",), kinds=("message", "skill", "read"))
    hooks = generate(pol("e-stop e-post e-pre"))["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == "Bash|Agent|Task|Skill"
    assert hooks["PreToolUse"][0]["matcher"] == claude_code.MATCHER + "|Skill|SendMessage"
    assert "gate event" in commands(hooks["SubagentStop"])[0]
    assert [("gate event" in c) for c in commands(hooks["Stop"])] == [True, False]  # then the phone alert
    assert [("gate event" in c) for c in commands(hooks["UserPromptSubmit"])] == [False, True]


def test_the_pre_tool_use_matcher_grows_only_for_the_kinds_a_guard_reads(monkeypatch):
    fake_guard(monkeypatch, "e-agent", events=("pre",), kinds=("agent",))
    assert generate(pol("e-agent"))["hooks"]["PreToolUse"][0]["matcher"] == claude_code.MATCHER + "|Agent|Task"
    assert generate(pol("bulk-staging"))["hooks"]["PreToolUse"][0]["matcher"] == claude_code.MATCHER


def test_protect_adds_a_path_to_the_deny_rules_the_hook_and_the_sandbox():
    s = generate(pol(), protect=("/home/u/.claude/settings.json",))
    assert "Edit(//home/u/.claude/settings.json)" in s["permissions"]["deny"]
    assert "/home/u/.claude/settings.json" in s["sandbox"]["filesystem"]["denyWrite"]
    check = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--protect /home/u/.claude/settings.json" in check and check.endswith("|| exit 2")
    plain = generate(pol())
    assert "/home/u/.claude/settings.json" not in json.dumps(plain)
    shim = opencode.generate(pol(), ["ws"], _P("out"), _P("p.toml"), protect=("/home/u/x",))
    assert "/home/u/x" in shim[opencode.PLUGIN] and "/home/u/x" in shim[opencode.CONFIG]


def test_opencode_names_every_guard_it_does_not_run_yet():
    assert set(opencode.UNSUPPORTED_GUARDS) == set(guards.names())
    assert not hasattr(claude_code, "UNSUPPORTED_GUARDS")
    assert adapters.get("claude-code").EVENT_NAMES["PostToolUse"] == "post"


def test_the_commented_default_policy_generates_and_a_settings_file_round_trips(home, tmp_path, capsys):
    assert cli.main(["gate", "init"]) == 0
    out = tmp_path / "out"
    assert cli.main(["gate", "generate", "--runner", "claude-code", "--out", str(out)]) == 0
    hooks = json.loads((out / "settings.json").read_text())["hooks"]
    assert {"PostToolUse", "SessionStart", "UserPromptSubmit"} <= set(hooks)
    assert cli.main(["gate", "generate", "--runner", "claude-code", "--out", str(out)]) == 0
    assert "0 changes" in capsys.readouterr().out


def test_the_generated_post_tool_use_hook_reports_a_syntax_error_to_the_model(home, tmp_path):
    assert cli.main(["gate", "init"]) == 0
    out = tmp_path / "out"
    ws_cmd = shlex.join([sys.executable, "-m", "ws"])
    assert cli.main(["gate", "generate", "--runner", "claude-code", "--out", str(out), "--ws-cmd", ws_cmd]) == 0
    hook = json.loads((out / "settings.json").read_text())["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n")
    env = {"PATH": "/usr/bin:/bin", "WS_HOME": str(home / "wshome"), "HOME": str(home / "home")}
    payload = post(path=str(bad))
    done = subprocess.run(
        hook, shell=True, input=json.dumps(payload), capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 2 and "line 1" in done.stderr
    bad.write_text("x = 1\n")
    done = subprocess.run(
        hook, shell=True, input=json.dumps(payload), capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 0


# --- preflight only for a session with the sandbox block ----------------------------------------------------------


def test_preflight_is_skipped_only_for_settings_marked_as_having_no_sandbox_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True)
    (d / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"enabled": False}}))
    payload = {"cwd": str(d)}
    monkeypatch.delenv("WS_SANDBOXED", raising=False)  # settings written before the marker: the block, no marker
    assert "enabled = false" in claude_code.preflight(payload)
    monkeypatch.setenv("WS_SANDBOXED", "1")
    assert "enabled = false" in claude_code.preflight(payload)
    raw = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "cwd": str(d)})
    assert "enabled = false" in gate.check("claude-code", raw, pol())
    monkeypatch.setenv("WS_SANDBOXED", "0")
    assert claude_code.preflight(payload) is None
    assert gate.check("claude-code", raw, pol()) is None
    monkeypatch.delenv("WS_SANDBOXED")
    assert "enabled = false" in gate.check("claude-code", raw, pol())


def test_every_generated_hook_but_pre_and_post_tool_use_ends_in_true(monkeypatch):
    fake_guard(monkeypatch, "e-all", events=("start", "prompt", "stop", "subagent-stop", "post"), kinds=("command",))
    hooks = generate(pol("e-all"))["hooks"]
    assert set(hooks) == {
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SubagentStop",
        "Notification",
    }
    for name, groups in hooks.items():
        for cmd in commands(groups):
            if name == "PreToolUse":
                assert cmd.endswith("|| exit 2")
            elif name == "PostToolUse":
                assert cmd.endswith("--policy /x/wshome/policy.toml")  # exit 2 is the message to the model
            else:
                assert cmd.endswith("|| true"), (name, cmd)


def test_the_sandbox_marker_is_in_the_generated_env_beside_the_gated_marker():
    assert approvals.SANDBOX_MARKER == "WS_SANDBOXED"
    assert (
        generate(pol())["env"][approvals.SANDBOX_MARKER] == "1" and generate(pol())["env"][approvals.ENV_MARKER] == "1"
    )
