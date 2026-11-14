"""The gate's one decision path: the rules first, then the guards in a fixed order, the first deny ending it, each guard's
verdict in a log, and the command and the daemon sharing the code that decides (`gate.run_hook`)."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ws import cli, gate, guards, jsonl, paths, policy
from ws.guards import Finding


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def pol(on: str = "", extra: str = "") -> policy.Policy:
    names = ", ".join(f'"{n}"' for n in on.split())
    return policy.loads(f"[guards]\non = [{names}]\n{extra}" if on or extra else "")


def bash(cmd, **extra):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1", "cwd": "/", **extra})


def fake(monkeypatch, calls: list, name: str, *, priority=None, finding=None, events=("pre",), kinds=("command",)):
    """A guard that notes in `calls` that it was asked, and returns `finding`."""

    def judge(call, cfg, ctx):
        calls.append(name)
        return finding

    attrs = {"PRIORITY": priority} if priority is not None else {}
    mod = SimpleNamespace(
        NAME=name,
        EVENTS=events,
        KINDS=kinds,
        config=lambda table, where: guards.known_keys(table, where),
        judge=judge,
        **attrs,
    )
    real = guards.all_guards
    monkeypatch.setattr(guards, "all_guards", lambda: {**real(), name: mod})
    return mod


# --- the order ---------------------------------------------------------------------------------------------------


def test_guards_run_by_priority_then_name_whatever_order_the_policy_lists_them(home, monkeypatch):
    calls: list[str] = []
    for name, priority in (("late", 90), ("early", 0), ("b-mid", 10), ("a-mid", 10), ("plain", None)):
        fake(monkeypatch, calls, name, priority=priority)
    assert gate.check("claude-code", bash("ls"), pol("plain late b-mid early a-mid")) is None
    assert calls == ["early", "a-mid", "b-mid", "plain", "late"]  # 0, 10 (by name), the default 50, 90
    calls.clear()
    assert gate.check("claude-code", bash("ls"), pol("early a-mid b-mid late plain")) is None
    assert calls == ["early", "a-mid", "b-mid", "plain", "late"]  # the same for another listing


def test_every_shipped_guard_declares_where_it_runs():
    for name, mod in guards.all_guards().items():
        assert isinstance(mod.PRIORITY, int) and not isinstance(mod.PRIORITY, bool), name
    assert guards.get("record-calls").PRIORITY == min(g.PRIORITY for g in guards.all_guards().values())
    assert guards.priority(SimpleNamespace()) == guards.DEFAULT_PRIORITY  # a guard that declares none
    assert guards.priority(SimpleNamespace(PRIORITY="high")) == guards.DEFAULT_PRIORITY


def test_the_rules_of_the_policy_are_asked_before_any_guard(home, monkeypatch):
    calls: list[str] = []
    fake(monkeypatch, calls, "watcher", priority=0)
    p = policy.loads('[gate]\ndeny_commands = ["make deploy"]\n[guards]\non = ["watcher"]\n')
    layer: list[str] = []
    assert "deny_commands" in gate.check("claude-code", bash("make deploy"), p, layer=layer)
    assert calls == [] and layer == ["rule:command"]  # the rule refused it: not one guard was asked


def test_the_first_deny_ends_it_and_a_later_guard_is_not_asked(home, monkeypatch):
    calls: list[str] = []
    fake(monkeypatch, calls, "first", priority=10, finding=Finding("deny", "no, says first"))
    fake(monkeypatch, calls, "second", priority=20, finding=Finding("deny", "no, says second"))
    verdicts: list[dict] = []
    layer: list[str] = []
    reason = gate.check("claude-code", bash("ls"), pol("second first"), layer=layer, verdicts=verdicts)
    assert reason == "first: no, says first" and layer == ["guard:first"]
    assert calls == ["first"]
    assert [(v["guard"], v["result"]) for v in verdicts] == [("first", "deny")]  # the one that refused is on record


def test_a_warning_does_not_end_it_and_reaches_the_notes(home, monkeypatch):
    calls: list[str] = []
    fake(monkeypatch, calls, "adviser", priority=10, finding=Finding("warn", "mind that"))
    fake(monkeypatch, calls, "judge", priority=20, finding=Finding("deny", "no"))
    notes: list[str] = []
    verdicts: list[dict] = []
    assert gate.check("claude-code", bash("ls"), pol("adviser judge"), notes=notes, verdicts=verdicts) == "judge: no"
    assert notes == ["adviser: mind that"] and calls == ["adviser", "judge"]
    assert [(v["guard"], v["result"]) for v in verdicts] == [("adviser", "warn"), ("judge", "deny")]


def test_a_recording_guard_sees_the_call_a_later_guard_refuses(home, monkeypatch):
    p = policy.loads('[guards]\non = ["bulk-staging", "record-calls"]\n')  # listed last, runs first
    layer: list[str] = []
    assert gate.check("claude-code", bash("git add -A"), p, layer=layer, home=home / "state")
    assert layer == ["guard:bulk-staging"]
    lines = jsonl.read(home / "state" / "logs" / "gate-calls.jsonl")
    assert len(lines) == 1 and lines[0]["payload"]["tool_input"]["command"] == "git add -A"


def test_a_guard_that_raises_is_an_error_verdict_and_the_call_is_refused(home, monkeypatch):
    def boom(call, cfg, ctx):
        raise RuntimeError("broken guard")

    fake(monkeypatch, [], "broken", priority=10).judge = boom
    verdicts: list[dict] = []
    with pytest.raises(RuntimeError):
        gate.check("claude-code", bash("ls"), pol("broken"), verdicts=verdicts)
    assert [(v["guard"], v["result"]) for v in verdicts] == [("broken", "error")]


def test_the_other_events_run_every_guard_in_the_same_order(home, monkeypatch):
    calls: list[str] = []
    fake(monkeypatch, calls, "z-stop", priority=5, events=("stop",), kinds=())
    fake(monkeypatch, calls, "a-stop", priority=50, events=("stop",), kinds=())
    verdicts: list[dict] = []
    gate.event(
        "claude-code",
        json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
        pol("a-stop z-stop"),
        verdicts=verdicts,
    )
    assert calls == ["z-stop", "a-stop"] and [v["guard"] for v in verdicts] == ["z-stop", "a-stop"]


# --- one decision path for the command and the daemon ------------------------------------------------------------


def pin_args() -> list[str]:
    return paths.pin_of().args()


def via_command(monkeypatch, capsys, kind, raw, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = cli.main(["gate", kind, "--runner", "claude-code", *pin_args(), *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize(
    "raw",
    [
        bash("ls"),
        bash("git push --force origin main"),
        bash("cat ~/.ssh/id_rsa"),
        json.dumps({"tool_name": "Read", "tool_input": {"file_path": "~/.ssh/id_rsa"}, "cwd": "/"}),
        "not json",
        json.dumps(["a list"]),
    ],
)
def test_run_hook_answers_exactly_as_the_command_does(home, monkeypatch, capsys, raw):
    code, out, err = via_command(monkeypatch, capsys, "check", raw)
    assert gate.run_hook("check", ["--runner", "claude-code", *pin_args()], raw) == (code, out, err)


def test_run_hook_for_an_event_answers_exactly_as_the_command_does(home, monkeypatch, capsys):
    raw = json.dumps({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/"})
    code, out, err = via_command(monkeypatch, capsys, "event", raw)
    assert gate.run_hook("event", ["--runner", "claude-code", *pin_args()], raw) == (code, out, err)


def test_run_hook_with_an_option_the_command_doesnt_take_exits_as_the_command_does(home):
    with pytest.raises(SystemExit):
        gate.run_hook("check", ["--runner", "claude-code", "--bogus"], bash("ls"))
    with pytest.raises(SystemExit):
        gate.run_hook("check", ["--runner", "nope"], bash("ls"))
    with pytest.raises(ValueError):
        gate.run_hook("launch", ["--runner", "claude-code"], bash("ls"))


def test_a_hook_whose_pinned_folders_do_not_match_the_environment_is_a_deny(home, monkeypatch):
    other = ["--pin", f"home={home / 'elsewhere'}", "--pin", f"config={home}", "--pin", f"state={home}"]
    code, _out, err = gate.run_hook(
        "check", ["--runner", "claude-code", *other, "--pin", f"data={home}", "--pin", f"cache={home}"], bash("ls")
    )
    assert code == gate.DENY and "blocked" in err


# --- the verdict log ---------------------------------------------------------------------------------------------


def test_a_hook_run_that_asks_a_guard_leaves_one_line_with_each_verdict(home):
    policy_file = home / "wshome" / "policy.toml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text('[guards]\non = ["dev-server", "bulk-staging"]\n')
    argv = ["--runner", "claude-code", "--policy", str(policy_file), *pin_args()]
    assert gate.run_hook("check", argv, bash("ls"))[0] == 0
    assert gate.run_hook("check", argv, bash("git add -A"))[0] == gate.DENY
    log = jsonl.read(paths.state_dir() / "logs" / gate.VERDICT_LOG)
    allow, deny = log
    assert (
        allow["schema"] == gate.VERDICT_SCHEMA
        and allow["ts"]
        and allow["decision"] == "allow"
        and allow["layer"] is None
    )
    assert [(v["guard"], v["result"]) for v in allow["guards"]] == [("bulk-staging", "pass"), ("dev-server", "pass")]
    assert deny["decision"] == "deny" and deny["layer"] == "guard:bulk-staging" and deny["via"] == "direct"
    assert [(v["guard"], v["result"]) for v in deny["guards"]] == [("bulk-staging", "deny")]  # dev-server was not asked
    assert {"runner", "session", "event", "ms"} <= set(deny)


def test_a_refusal_by_a_rule_is_logged_without_the_command(home):
    secret = "tok" + "en-" + "abc123"  # built at run time: no secret-shaped text in the file
    argv = ["--runner", "claude-code", *pin_args()]
    assert gate.run_hook("check", argv, bash(f"cat ~/.ssh/id_rsa # {secret}"))[0] == gate.DENY
    text = (paths.state_dir() / "logs" / gate.VERDICT_LOG).read_text()
    record = json.loads(text)
    assert record["decision"] == "deny" and record["layer"] == "rule:command" and secret not in text
    assert "id_rsa" not in text


def test_a_plain_allow_with_no_guard_asked_writes_one_line_with_no_rule(home):
    policy_file = home / "wshome" / "policy.toml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text("")
    argv = ["--runner", "claude-code", "--policy", str(policy_file), *pin_args()]
    assert gate.run_hook("check", argv, bash("ls"))[0] == 0
    (line,) = jsonl.read(paths.state_dir() / "logs" / gate.VERDICT_LOG)
    assert line["decision"] == "allow" and line["guards"] == [] and "rule" not in line and "unlisted" not in line


def test_a_log_that_cannot_be_written_never_changes_the_decision(home, monkeypatch):
    def full(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(jsonl, "append", full)
    argv = ["--runner", "claude-code", *pin_args()]
    assert gate.run_hook("check", argv, bash("cat ~/.ssh/id_rsa"))[0] == gate.DENY
    assert gate.run_hook("check", argv, bash("ls"))[0] == 0


# --- the page says what the code declares -------------------------------------------------------------------------


def test_the_guards_page_lists_each_guards_priority_as_the_code_declares_it():
    page = Path(__file__).resolve().parents[1] / "docs" / "reference" / "guards.md"
    section = page.read_text(encoding="utf-8").split("## The order guards run in")[1].split("\n## ")[0]
    listed = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0].isdigit():
            listed.update({name: int(cells[0]) for name in re.findall(r"`([a-z-]+)`", cells[2])})
    assert listed == {name: mod.PRIORITY for name, mod in guards.all_guards().items()}


# --- the clocks: the runner's hook time-out holds the client's whole worst case ----------------------------------


def test_the_hook_time_out_is_longer_than_the_clients_whole_worst_case():
    """The client waits for the daemon, then decides in its own process. If the runner's time-out is shorter than that,
    it kills a hook that was about to answer and onFailure "block" denies a call that should have been allowed."""
    from ws import gateclient, gateserve
    from ws.adapters import claude_code
    from ws.guards import claims, syntax_check

    assert gateclient.FALLBACK_S >= max(claims.GIT_BUDGET_S, syntax_check.TIMEOUT_S) + 1  # and a second to start
    assert gateserve.HARD_LIMIT < gateclient.REPLY_TIMEOUT  # the daemon gives up on a request before the client does
    assert gateclient.WORST_CASE_S == gateclient.CONNECT_TIMEOUT + gateclient.REPLY_TIMEOUT + gateclient.FALLBACK_S
    assert gateclient.WORST_CASE_S < claude_code.HOOK_TIMEOUT


def test_every_blocking_hook_ws_writes_has_the_time_out_and_fails_closed(home):
    from ws.adapters import claude_code, opencode

    pol = policy.loads('[guards]\non = ["syntax-check"]\n')
    pin = paths.pin_of()
    files = claude_code.generate(pol, ["ws"], home / "out", home / "policy.toml", pin=pin)
    hooks = json.loads(files["settings.json"])["hooks"]
    blocking = [h for event in ("PreToolUse", "PostToolUse") for g in hooks[event] for h in g["hooks"]]
    assert len(blocking) >= 2
    for h in blocking:
        assert h["timeout"] == claude_code.HOOK_TIMEOUT and h["onFailure"] == "block", h
    plugin = opencode.generate(pol, ["ws"], home / "out", home / "policy.toml", pin=pin)[opencode.PLUGIN]
    assert f"timeout: {claude_code.HOOK_TIMEOUT * 1000}," in plugin  # OpenCode's check is killed on the same clock


def test_the_claims_guard_spends_its_git_time_inside_the_clients_fallback_window():
    from ws import gateclient
    from ws.guards import claims

    assert claims.GIT_BUDGET_S < gateclient.FALLBACK_S


# --- the knob that runs the gate caseless -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected", [("1", True), ("0", sys.platform == "darwin"), (None, sys.platform == "darwin")]
)
def test_the_caseless_knob_can_only_turn_caseless_matching_on(value, expected):
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != gate.CASELESS_ENV}
    if value is not None:
        env[gate.CASELESS_ENV] = value
    done = subprocess.run(
        [sys.executable, "-I", "-c", "from ws import gate, policy; print(policy.CASELESS_PATHS)"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.stdout.strip() == str(expected), done.stderr
