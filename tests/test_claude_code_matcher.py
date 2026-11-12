"""The PreToolUse gate hook is registered for every tool, so a tool the runner adds later is gated too, and the hook still
runs and denies whatever permission mode the session is in. The matcher rules are Claude Code's hooks reference ("Matcher
patterns"): `*`, an empty string or no matcher is every tool; letters, digits, `_`, `-`, spaces, `,` and `|` only is an
exact list; anything else is an unanchored JavaScript regular expression."""

from __future__ import annotations

import json
import re

import pytest

from ws import gate, paths, policy
from ws.adapters import claude_code

UNLISTED = "FutureTool"  # a tool name nothing in ws has heard of


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    (tmp_path / "wshome").mkdir()
    return tmp_path


def matches(matcher: str | None, tool: str) -> bool:
    """Would Claude Code run a hook group with this `matcher` for `tool`?"""
    if matcher in (None, "", "*"):
        return True
    if re.fullmatch(r"[\w\- ,|]+", matcher):
        return tool in [x.strip() for x in re.split(r"[|,]", matcher)]
    return re.search(matcher, tool) is not None


def settings(box, body: str = "", pol: policy.Policy | None = None) -> dict:
    pol = pol or policy.loads("[gate]\n" + body)
    files = claude_code.generate(pol, ["ws"], box / "out", box / "wshome" / "policy.toml", pin=paths.pin_of())
    return json.loads(files["settings.json"])


def payload(tool: str, tool_input: dict | None = None, mode: str | None = None) -> str:
    p = {"tool_name": tool, "tool_input": {} if tool_input is None else tool_input, "session_id": "s", "cwd": "/"}
    if mode:
        p["permission_mode"] = mode
    return json.dumps(p)


def test_the_generated_settings_match_a_tool_ws_has_never_heard_of(box):
    (group,) = settings(box)["hooks"]["PreToolUse"]
    assert matches(group["matcher"], UNLISTED)
    assert group["hooks"][0]["onFailure"] == "block" and group["hooks"][0]["command"].endswith("|| exit 2")


@pytest.mark.parametrize("tool", ["Bash", "Read", "Edit", "mcp__x__y", "Agent", "SendMessage", "ListAgents", UNLISTED])
def test_the_matcher_covers_every_kind_of_tool(box, tool):
    assert matches(settings(box)["hooks"]["PreToolUse"][0]["matcher"], tool)


def test_the_matcher_is_every_tool_whatever_the_policy_asks_for(box):
    assert claude_code.MATCHER == "*"
    for body in ("", 'deny_tools = ["web_fetch", "mcp__x__*"]\n', "allow_mcp = []\n"):
        assert settings(box, body)["hooks"]["PreToolUse"][0]["matcher"] == claude_code.MATCHER


def test_the_event_hooks_keep_their_own_narrower_matcher(box):
    hooks = settings(box, pol=policy.default_policy())["hooks"]
    post = hooks.get("PostToolUse")
    assert post and all(g["matcher"] != "*" for g in post)  # _matcher still builds the event matchers


def test_the_gate_allows_a_tool_it_does_not_know_without_raising(box):
    pol = policy.default_policy()
    for tool_input in ({}, {"anything": 1, "nested": {"a": [1, 2]}}):
        assert gate.check("claude-code", payload(UNLISTED, tool_input), pol) is None
    assert claude_code.parse(json.loads(payload(UNLISTED, {"x": "y"}))) is None  # not a call ws judges: allowed


def test_a_known_tool_is_still_judged_beside_an_unknown_one(box):
    pol = policy.default_policy()
    assert gate.check("claude-code", payload("Bash", {"command": "git push --force origin main"}), pol)


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto", "default", "plan", "acceptEdits"])
def test_the_hook_denies_a_denied_command_in_every_permission_mode(box, mode):
    pol = policy.default_policy()
    reason = gate.check("claude-code", payload("Bash", {"command": "git push --force origin main"}, mode), pol)
    assert isinstance(reason, str) and not isinstance(reason, gate.Ask)  # a deny, not a request for approval


def test_the_hook_allows_a_plain_command_in_bypass_mode_too(box):
    """The mode changes nothing either way: the gate reads the call, not the mode."""
    pol = policy.default_policy()
    assert gate.check("claude-code", payload("Bash", {"command": "ls"}, "bypassPermissions"), pol) is None
