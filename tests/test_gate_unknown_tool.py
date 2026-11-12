"""A tool no adapter knows is not invisible: the gate makes one Call of kind "other" from its name, allows it, and leaves
one counts-only line in the verdict log. An MCP call reaches the gate (the hook matches every tool) and is recorded
beside the rest. Tool names and arguments here are made up; nothing is read from the machine."""

from __future__ import annotations

import json

import pytest

from ws import adapters, gate, paths, policy
from ws.adapters import Call, claude_code
from ws.guards import record_calls


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    (tmp_path / "wshome").mkdir()
    return tmp_path


def run(runner: str, payload: dict, policy_file=None) -> tuple[int, str, str]:
    argv = ["--runner", runner, *paths.pin_of().args(), *(["--policy", str(policy_file)] if policy_file else [])]
    return gate.run_check(gate.hook_args("check", argv), json.dumps(payload))


def verdict_lines() -> list[dict]:
    log = paths.state_dir() / "logs" / gate.VERDICT_LOG
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_an_unlisted_tool_is_logged_and_allowed(box):
    secret_text = "plan-" + "text-7731"  # what the tool was given: it never reaches the log
    payload = {"tool_name": "PlanTool", "tool_input": {"notes": secret_text}, "session_id": "s1", "cwd": str(box)}
    assert claude_code.parse(payload) is None  # no adapter reads it
    assert gate._calls(adapters.get("claude-code"), payload) == [Call("other", "PlanTool")]
    assert gate.evaluate(policy.default_policy(), Call("other", "PlanTool")) is None  # the policy has no rule for it
    code, out, _ = run("claude-code", payload)
    assert (code, out) == (0, "")
    (line,) = verdict_lines()
    assert line["decision"] == "allow" and line["session"] == "s1" and line["event"] == "PreToolUse"
    assert line["tool"] == "PlanTool" and line["unlisted"] == 1 and line["layer"] is None
    assert isinstance(line["ms"], float)
    assert secret_text not in json.dumps(line) and str(box) not in json.dumps(line)


def test_the_opencode_shim_names_its_tool_in_the_same_way(box):
    payload = {"tool": "todowrite", "args": {"todos": []}, "session_id": "s2", "cwd": str(box)}
    assert gate._calls(adapters.get("opencode"), payload) == [Call("other", "todowrite")]
    code, _, _ = run("opencode", payload)
    assert code == 0
    (line,) = verdict_lines()
    assert line["tool"] == "todowrite" and line["unlisted"] == 1 and line["runner"] == "opencode"


def test_the_name_is_capped_and_a_payload_with_no_readable_name_is_still_a_deny(box):
    long_name = "T" * 500
    (call,) = gate._calls(adapters.get("claude-code"), {"tool_name": long_name, "tool_input": {}})
    assert call.kind == "other" and len(call.value) == gate.MAX_TOOL_NAME
    # no name, or a name that is not text: parse still raises, and the gate fails closed as before
    for bad in ({"tool_input": {}}, {"tool_name": 7, "tool_input": {}}, {"tool_name": "Foo", "tool_input": "x"}):
        code, _, err = run("claude-code", bad)
        assert code == gate.DENY and "could not check this call" in err, bad
    assert not [x for x in verdict_lines() if x.get("unlisted")]


def test_a_known_tool_leaves_no_unlisted_line(box):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}, "session_id": "s3", "cwd": str(box)}
    plain = box / "plain.toml"
    plain.write_text("[gate]\n")  # no guard on, so none is asked
    assert run("claude-code", payload, plain)[0] == 0
    assert verdict_lines() == []  # an ordinary allowed call with no guard asked writes nothing


def test_a_planted_mcp_call_reaches_the_gate_and_is_recorded(box):
    """The hook matches every tool, so an MCP call arrives; with record-calls on it is in gate-calls.jsonl, and
    `ws gate replay` can run the line again."""
    pol = box / "policy.toml"
    pol.write_text('[gate]\nallow_mcp = ["mcp__sample__*"]\n[guards]\non = ["record-calls"]\n')
    mcp = {
        "tool_name": "mcp__sample__lookup",
        "tool_input": {"query": "widgets"},
        "session_id": "s4",
        "cwd": str(box),
    }
    other = {"tool_name": "PlanTool", "tool_input": {"n": 1}, "session_id": "s4", "cwd": str(box)}
    assert {"mcp", "other"} <= set(record_calls.KINDS)
    settings = json.loads(claude_code.generate(policy.load(pol), ["ws"], box / "out", pol)["settings.json"])
    assert [g["matcher"] for g in settings["hooks"]["PreToolUse"]] == ["*"]  # every tool reaches the hook
    for payload in (mcp, other):
        assert run("claude-code", payload, pol)[0] == 0
    log = record_calls.log_path()
    recorded = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["payload"]["tool_name"] for r in recorded] == ["mcp__sample__lookup", "PlanTool"]
    replayed = gate.replay("claude-code", log.read_text().splitlines(), policy.load(pol))
    assert [r["result"] for r in replayed] == ["allow", "allow"]
    # an MCP call the policy does not name is still asked for, and recorded all the same
    unnamed = dict(mcp, tool_name="mcp__elsewhere__lookup")
    assert run("claude-code", unnamed, pol)[0] == gate.DENY
    assert len(log.read_text().splitlines()) == 3
