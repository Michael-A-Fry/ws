"""The CLI contract: every command takes --json and answers with one envelope; errors are typed, with a hint.

real parser, so a new command fails here until it is given a case (or is listed as raw, with a reason).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

import pytest

from ws import cli, output

# Commands that keep their own stdout (a protocol, a hook, the terminal) and so are not wrapped; their errors still are.
RAW = {
    "gate.check": "a hook: the runner reads its stdout, stderr and exit code",
    "gate.launch": "hands the terminal to the runner",
    "mcp.serve": "stdout is the MCP protocol",
    "broker.serve": "stays up until stopped",
    "session.start": "a hook entry",
    "session.nudge": "a hook entry",
    "setup": "a person at a terminal, or the page (setup --yes is covered below)",
}

# What to run for each of the others (in a throwaway HOME): enough to get a real answer or a typed error.
CASES: dict[str, list[str]] = {
    "help": ["help", "doctor"],
    "doctor": ["doctor", "--offline"],
    "init": ["init", "--yes", "--create-notes", "--notes", "{home}/Notes"],
    "gate.init": ["gate", "init"],
    "gate.generate": ["gate", "generate", "--runner", "claude-code", "--out", "{home}/gate"],
    "secret.set": ["secret", "set", "nope"],
    "secret.list": ["secret", "list"],
    "secret.rm": ["secret", "rm", "nope"],
    "secret.wall": ["secret", "wall"],
    "broker.request": ["broker", "request", "x", "--purpose", "p", "--host", "h", "--socket", "{home}/none.sock"],
    "broker.status": ["broker", "status", "--socket", "{home}/none.sock"],
    "note": ["note", "hello"],
    "memory.init": ["memory", "init"],
    "memory.add": ["memory", "add", "a fact"],
    "memory.search": ["memory", "search", "fact"],
    "memory.reindex": ["memory", "reindex"],
    "run": ["run", "nojob", "--dry-run"],
    "runs": ["runs"],
    "globals.check": ["globals", "check", "--repo", "{home}"],
    "globals.manifest": ["globals", "manifest"],
    "install": ["install", "--dry-run", "--notes", "{home}/Notes"],
    "uninstall": ["uninstall", "--dry-run"],
    "plugin.list": ["plugin", "list"],
    "plugin.add": ["plugin", "add", "nope"],
    "plugin.remove": ["plugin", "remove", "nope"],
    "module.list": ["module", "list"],
    "module.add": ["module", "add", "nope"],
    "module.remove": ["module", "remove", "nope"],
    "device.add": ["device", "add"],
    "skills.list": ["skills", "list"],
    "skills.add": ["skills", "add", "align"],
    "canary": ["canary"],
    "rate": ["rate", "last", "up"],
    "notify.test": ["notify", "test"],
    "notify.send": ["notify", "send", "hi"],
    "notify.status": ["notify", "status"],
    "backup.init": ["backup", "init"],
    "backup.run": ["backup", "run"],
    "backup.verify": ["backup", "verify"],
    "schedule.list": ["schedule", "list"],
    "schedule.apply": ["schedule", "apply", "--dry-run"],
    "schedule.remove": ["schedule", "remove", "nojob"],
    "approve": ["approve", "--list"],
    "config": ["config"],
    "status": ["status"],
    "eval.run": ["eval", "run", "--set", "starter", "--dry-run"],
    "eval.report": ["eval", "report"],
    "eval.compare": ["eval", "compare", "a", "b"],
    "eval.harvest": ["eval", "harvest"],
    "eval.approve": ["eval", "approve", "nothing"],
    "eval.reject": ["eval", "reject", "nothing"],
}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home.mkdir()
    bare = tmp_path / "bin"
    bare.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("PATH", str(bare))  # no claude, opencode, tailscale, git or systemctl to call
    monkeypatch.delenv("WS_OUTPUT", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(home)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return home


def leaves() -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}

    def walk(p: argparse.ArgumentParser) -> None:
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            found[p.get_default("ws_command")] = p
        for s in subs:
            for child in s.choices.values():
                walk(child)

    walk(cli.build_parser())
    return found


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def envelope(text: str) -> dict:
    env = json.loads(text)  # the whole of stdout is one JSON document
    assert env["schema"] == output.SCHEMA
    return env


def test_every_command_is_covered_or_listed_raw():
    kinds = set(leaves())
    assert kinds - set(CASES) - set(RAW) == set(), "a new command needs a CASES entry (or a reason in RAW)"
    assert (set(CASES) | set(RAW)) - kinds == set(), "a CASES or RAW entry names a command that is gone"


@pytest.mark.parametrize("kind", sorted(CASES))
def test_json_gives_one_envelope(box, capsys, kind):
    argv = [a.format(home=box) for a in CASES[kind]]
    code, out, _ = run(capsys, *argv, "--json")
    env = envelope(out)
    assert env["kind"] == kind
    if env["status"] == "error":
        assert code == env["exit_code"] != 0
        assert set(env) >= {"error_kind", "message", "hint"} and env["error_kind"] and env["message"]
    else:
        assert env["status"] in (output.OK, output.ATTENTION) and "data" in env
        assert (env["status"] == output.ATTENTION) == (code != 0)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_ws_output_env_is_the_same_as_the_flag(box, capsys, monkeypatch, kind):
    monkeypatch.setenv("WS_OUTPUT", "json")
    _, out, _ = run(capsys, *[a.format(home=box) for a in CASES[kind]])
    assert envelope(out)["kind"] == kind


def test_json_works_before_the_command_too(box, capsys):
    code, out, _ = run(capsys, "--json", "config")
    assert code == 0 and envelope(out)["kind"] == "config"


def test_json_after_dashes_is_the_commands_own(box, capsys):
    code, out, err = run(capsys, "note", "--", "--json")  # a note whose text is "--json"
    assert code == 1 and "no [roles.notes]" in err and out == ""


@pytest.mark.parametrize("kind", sorted(RAW))
def test_raw_commands_are_listed_with_a_reason(kind):
    assert RAW[kind]


def test_raw_commands_give_their_errors_as_text_on_stderr(box, capsys, monkeypatch):
    code, out, err = run(capsys, "gate", "launch", "--runner", "opencode", "--json")
    assert code == 1 and out == "" and err.splitlines()[0].startswith("ws: give the runner's command")
    assert "hint:" in err
    monkeypatch.setenv("WS_OUTPUT", "json")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("not json")
    )  # a payload the hook cannot read: denied, reason on stderr
    code, out, err = run(capsys, "gate", "check", "--runner", "claude-code")
    assert code == 2 and out == "" and "blocked" in err


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("mode", ["flag", "env"])
def test_a_prompt_is_seen_under_json_and_the_answer_is_honoured(box, capsys, monkeypatch, mode):
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    argv = ["uninstall"]
    if mode == "env":
        monkeypatch.setenv("WS_OUTPUT", "json")
    else:
        argv.append("--json")
    code, out, _ = run(capsys, *argv)
    assert code == 1 and "Remove what ws installed? [y/N]" in out  # not swallowed into an envelope
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    code, out, _ = run(capsys, "init", "--json", "--create-notes")
    assert "Does this machine stay on all the time?" in out and '"schema": "ws.cli/1"' not in out.split("\n")[0]


def test_every_command_that_can_prompt_keeps_the_terminal():
    parser = cli.build_parser()
    ns = parser.parse_args(["approve", "abcdef12"])
    assert output._raw(ns) is True
    assert output._raw(parser.parse_args(["approve", "--list"])) is False
    assert output._raw(parser.parse_args(["uninstall"])) is True
    assert output._raw(parser.parse_args(["uninstall", "--dry-run"])) is False
    assert output._raw(parser.parse_args(["uninstall", "--yes"])) is False
    assert output._raw(parser.parse_args(["init", "--yes"])) is False


def test_the_fresh_doctor_script_reads_the_envelope(box, capsys, tmp_path):
    import subprocess

    _, out, _ = run(capsys, "doctor", "--json")
    report = tmp_path / "doctor.json"
    report.write_text(out)
    script = Path(__file__).resolve().parent.parent / "scripts" / "check_fresh_doctor.py"
    ok = subprocess.run([sys.executable, str(script), str(report), "1"], capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr + ok.stdout
    bare = tmp_path / "bare.json"  # the pre-envelope shape must not pass
    bare.write_text(json.dumps(json.loads(out)["data"]))
    old = subprocess.run([sys.executable, str(script), str(bare), "1"], capture_output=True, text=True, check=False)
    assert old.returncode != 0


def test_a_typed_error_has_kind_message_hint_and_exit_code(box, capsys):
    code, out, err = run(capsys, "note", "--json")  # no text
    env = envelope(out)
    assert code == 1 and err == ""
    assert env == {
        "schema": output.SCHEMA,
        "kind": "note",
        "status": "error",
        "error_kind": "bad_input",
        "message": "nothing to note: give some text",
        "hint": "or `-` to read it from stdin",
        "exit_code": 1,
    }


def test_text_errors_go_to_stderr_with_a_hint_line(box, capsys):
    code, out, err = run(capsys, "note")
    assert code == 1 and out == ""
    assert err.splitlines() == ["ws: nothing to note: give some text", "hint: or `-` to read it from stdin"]


def test_unknown_command_suggests_the_closest(box, capsys):
    code, _, err = run(capsys, "docter")
    assert code == 2 and "hint: did you mean `doctor`?" in err
    code, out, _ = run(capsys, "docter", "--json")
    env = envelope(out)
    assert code == 2 and env["error_kind"] == "usage" and "`doctor`" in env["hint"]


def test_unknown_subcommand_and_flag_suggest_too(box, capsys):
    code, _, err = run(capsys, "gate", "generate"[:-1])
    assert code == 2 and "did you mean `generate`?" in err
    code, _, err = run(capsys, "doctor", "--ofline")
    assert code == 2 and "did you mean `--offline`?" in err


def test_a_command_with_nothing_close_points_at_help(box, capsys):
    code, _, err = run(capsys, "zzzzzz")
    assert code == 2 and "hint: run `ws --help`" in err


def test_bad_cwd_is_invalid_cwd_before_anything_runs(box, capsys):
    gone = box / "nowhere"
    code, out, _ = run(capsys, "-C", str(gone), "doctor", "--json")
    env = envelope(out)
    assert code == 2 and env["error_kind"] == "invalid_cwd" and str(gone) in env["message"]
    assert env["details"]["path"] == str(gone)
    (box / "afile").write_text("x")
    code, out, _ = run(capsys, "--json", "-C", str(box / "afile"), "status")
    assert envelope(out)["error_kind"] == "invalid_cwd"
    code, _, err = run(capsys, "-C")
    assert code == 2 and "needs a folder" in err


def test_cwd_is_applied_before_dispatch(box, capsys):
    sub = box / "repo"
    sub.mkdir()
    code, out, _ = run(capsys, "-C", str(sub), "status", "--json")
    assert code == 0 and Path(envelope(out)["data"]["cwd"]) == sub.resolve()
    code, out, _ = run(capsys, "status", "--json", f"--cwd={box}")
    assert Path(envelope(out)["data"]["cwd"]) == box.resolve()


def test_help_is_json_per_topic(box, capsys):
    code, out, _ = run(capsys, "help", "gate", "--json")
    data = envelope(out)["data"]
    assert code == 0 and data["command"] == "ws gate"
    assert {c["name"] for c in data["commands"]} >= {"init", "generate", "check", "launch"}
    code, out, _ = run(capsys, "help", "--json")
    assert {"doctor", "config", "status"} <= {c["name"] for c in envelope(out)["data"]["commands"]}
    code, out, _ = run(capsys, "help", "gate", "generate", "--json")
    flags = [f for o in envelope(out)["data"]["options"] for f in o["flags"]]
    assert "--runner" in flags and "--out" in flags
    code, out, _ = run(capsys, "help", "gat", "--json")
    assert code == 2 and "`gate`" in envelope(out)["hint"]
    code, out, _ = run(capsys, "help", "gate")
    assert code == 0 and out.startswith("usage: ws gate")


def test_stdout_is_only_the_envelope_even_if_the_command_prints(box, capsys):
    _, out, _ = run(capsys, "globals", "manifest", "--json")  # prints prose or JSON of its own when not --json
    assert envelope(out)["kind"] == "globals.manifest"


def test_doctor_keeps_its_own_schema_inside_data(box, capsys):
    code, out, _ = run(capsys, "doctor", "--offline", "--json")
    env = envelope(out)
    assert code == 1 and env["status"] == "attention"
    assert env["data"]["schema"] == 1 and env["data"]["ok"] is False


# ---- setup and install never turn the dial to act ----------------------------------------------------------------------

ACT = re.compile(r"""dial\s*=\s*["']act["']""")


def written(home: Path) -> list[Path]:
    return [p for p in home.rglob("*") if p.is_file() and ".git" not in p.parts]


def test_setup_yes_and_install_never_write_dial_act(box, capsys):
    notes = box / "Notes"
    assert cli.main(["setup", "--yes", "--notes", str(notes)]) == 0
    assert cli.main(["install", "--yes", "--notes", str(notes)]) == 0
    capsys.readouterr()
    policy = box / ".ws" / "policy.toml"
    assert policy.is_file() and 'dial = "prepare"' in policy.read_text()
    files = written(box)
    assert files
    bad = [str(p) for p in files if ACT.search(p.read_text(errors="replace"))]
    assert bad == []


def test_setup_yes_json_is_one_envelope(box, capsys):
    code, out, _ = run(capsys, "setup", "--yes", "--notes", str(box / "Notes"), "--json")
    env = envelope(out)
    assert code == 0 and env["kind"] == "setup" and "output" in env["data"]
    assert not any(ACT.search(p.read_text(errors="replace")) for p in written(box))


def test_install_says_first_doctor(box, capsys):
    code, out, _ = run(capsys, "install", "--yes", "--notes", str(box / "Notes"))
    assert code == 0 and "first: `ws doctor`" in out
    code, out, _ = run(capsys, "init", "--yes", "--notes", str(box / "Notes"))
    assert "first: `ws doctor`" in out
