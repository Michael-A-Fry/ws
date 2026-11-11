"""The gate judges MCP tool calls (`mcp__SERVER__TOOL`): `deny_tools` takes their names and globs, a tool `allow_mcp` does
not name is asked for (and logged), and the path-like arguments of a call go through `deny_read` and, for a tool that
writes, ws's own protected files. Planted values are built at run time under tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import cli, gate, mcp_server, paths, policy
from ws.adapters import claude_code, opencode


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    (tmp_path / "wshome").mkdir()
    return tmp_path


def call(tool: str, tool_input=None, session="s1") -> dict:
    return {
        "tool_name": tool,
        "tool_input": {} if tool_input is None else tool_input,
        "session_id": session,
        "cwd": "/",
    }


def policy_of(gate_body: str = "") -> policy.Policy:
    return policy.loads("[gate]\n" + gate_body)


def verdict(pol: policy.Policy, payload, protect: tuple[str, ...] = (), root: Path | None = None) -> str:
    reason = gate.check("claude-code", json.dumps(payload), pol, protect, root, home=None)
    return "allow" if reason is None else "ask" if isinstance(reason, gate.Ask) else "deny"


def planted(box: Path) -> Path:
    """A folder the policy denies reading, with a file in it (named at run time)."""
    folder = box / ("sealed-" + "x1")
    folder.mkdir()
    (folder / "f").write_text("data")
    return folder


# --- the tool's name -----------------------------------------------------------------------------------------------


def test_an_mcp_tool_the_policy_does_not_name_is_asked_for_and_the_ask_is_logged(box, capsys):
    pol = policy_of()
    assert verdict(pol, call("mcp__x__y")) == "ask"
    args = gate.hook_args("check", ["--runner", "claude-code", *paths.pin_of().args()])
    code, _, err = gate.run_check(args, json.dumps(call("mcp__x__y")))
    assert code == gate.DENY and "mcp__x__y" in err and "allow_mcp" in err  # the runner holds the call
    lines = (paths.state_dir() / "logs" / gate.VERDICT_LOG).read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["decision"] == "ask" and rec["layer"] == "ask_mcp" and rec["runner"] == "claude-code"


def test_an_asked_mcp_call_runs_after_a_person_approves_that_exact_call(box):
    from ws import approvals

    pol = policy_of()
    root = box / "approvals"
    payload = call("mcp__x__y", {"q": "one"})
    first = gate.check("claude-code", json.dumps(payload), pol, (), root)
    assert isinstance(first, gate.Ask) and "ws approve" in first
    pending = next((root / "pending").glob("*.json"))
    rec = json.loads(pending.read_text())
    assert rec["bound"]["kind"] == "mcp" and "mcp__x__y" in rec["bound"]["value"] and '"one"' in rec["bound"]["value"]
    approvals.grant(root, rec["id"][:12])
    assert gate.check("claude-code", json.dumps(payload), pol, (), root) is None  # used up by that one call
    assert isinstance(gate.check("claude-code", json.dumps(payload), pol, (), root), gate.Ask)
    other = call("mcp__x__y", {"q": "two"})  # other arguments are another call
    assert isinstance(gate.check("claude-code", json.dumps(other), pol, (), root), gate.Ask)


def test_deny_tools_takes_an_mcp_name_and_a_glob(box):
    assert verdict(policy_of('deny_tools = ["mcp__x__y"]'), call("mcp__x__y")) == "deny"
    assert verdict(policy_of('deny_tools = ["mcp__x__y"]\nallow_mcp = ["mcp__x__z"]'), call("mcp__x__z")) == "allow"
    pol = policy_of('deny_tools = ["mcp__x__*"]')
    assert verdict(pol, call("mcp__x__y")) == "deny" and verdict(pol, call("mcp__x__other")) == "deny"
    assert verdict(pol, call("mcp__z__y")) == "ask"  # another server is only unnamed
    wide = policy_of('deny_tools = ["mcp__*__delete_*"]\nallow_mcp = ["mcp__*"]')
    assert verdict(wide, call("mcp__files__delete_all")) == "deny"
    assert verdict(wide, call("mcp__files__read_all")) == "allow"


def test_an_allowed_tool_passes_and_deny_beats_allow(box):
    pol = policy_of('allow_mcp = ["mcp__x__*"]')
    assert verdict(pol, call("mcp__x__y")) == "allow"
    assert verdict(pol, call("mcp__q__y")) == "ask"
    both = policy_of('allow_mcp = ["mcp__x__y"]\ndeny_tools = ["mcp__x__y"]')
    assert verdict(both, call("mcp__x__y")) == "deny"
    globbed = policy_of('allow_mcp = ["mcp__x__y"]\ndeny_tools = ["mcp__x__*"]')
    assert verdict(globbed, call("mcp__x__y")) == "deny"


def test_the_tools_ws_ships_are_allowed_only_while_the_policy_says_nothing(box):
    served = {f"mcp__ws__{t['name']}" for t in mcp_server.TOOLS}
    assert set(policy.MCP_SHIPPED) == served
    for tool in served:
        assert verdict(policy_of(), call(tool, {"query": "x"})) == "allow"
    assert verdict(policy_of(), call("mcp__ws__something_else")) == "ask"
    assert verdict(policy_of("allow_mcp = []"), call("mcp__ws__memory_search")) == "ask"  # its own list replaces them
    assert verdict(policy_of('deny_tools = ["mcp__ws__*"]'), call("mcp__ws__memory_search")) == "deny"
    assert verdict(policy.default_policy(), call("mcp__ws__memory_search")) == "allow"


# --- the arguments -------------------------------------------------------------------------------------------------


def sealed_policy(folder: Path, extra: str = 'allow_mcp = ["mcp__*"]\n') -> policy.Policy:
    return policy_of(f'deny_read = ["{folder}/**"]\n{extra}')


def test_a_path_argument_in_a_denied_folder_is_denied(box):
    folder = planted(box)
    pol = sealed_policy(folder)
    inside, outside = str(folder / "f"), str(box / "elsewhere")
    assert verdict(pol, call("mcp__fs__read", {"path": inside})) == "deny"
    assert verdict(pol, call("mcp__fs__read", {"args": {"files": [outside, inside]}})) == "deny"
    assert verdict(pol, call("mcp__fs__read", {"uri": "file://" + inside})) == "deny"
    assert verdict(pol, call("mcp__fs__read", {"uri": "file://localhost" + inside.replace("-", "%2D")})) == "deny"
    assert verdict(pol, call("mcp__fs__read", {"anything": inside})) == "deny"  # a string that starts like a path
    assert verdict(pol, call("mcp__fs__read", {"items": [{"deep": {"x": inside}}]})) == "deny"  # in a list in a map
    assert verdict(pol, call("mcp__fs__read", {"path": outside})) == "allow"
    assert verdict(pol, call("mcp__fs__read", {"uri": "https://example.invalid/" + folder.name})) == "allow"
    assert verdict(pol, call("mcp__fs__read", {"text": "plain words " + inside})) == "allow"  # not a path on its own


def test_a_path_argument_that_reaches_a_denied_folder_through_a_listing_is_denied(box):
    folder = planted(box)
    pol = sealed_policy(folder)
    assert verdict(pol, call("mcp__fs__list", {"directory": str(folder)})) == "deny"
    assert verdict(pol, call("mcp__fs__search", {"query": "x", "path": str(box)})) == "deny"  # a folder above it
    assert verdict(pol, call("mcp__fs__read", {"directory": str(box / "other")})) == "allow"


def test_a_path_in_a_map_key_and_a_newline_list_is_judged(box):
    folder = planted(box)
    pol = sealed_policy(folder)
    assert verdict(pol, call("mcp__fs__batch", {"entries": {str(folder / "f"): "text"}})) == "deny"
    assert verdict(pol, call("mcp__fs__batch", {"paths": "ok.txt\n" + str(folder / "f")})) == "deny"


def test_a_write_named_tool_at_a_protected_path_is_denied(box):
    protected = box / "wshome" / "policy.toml"
    pol = policy_of('allow_mcp = ["mcp__*"]')
    for tool in (
        "mcp__fs__write_file",
        "mcp__fs__edit",
        "mcp__fs__create_directory",
        "mcp__fs__delete",
        "mcp__fs__move_file",
    ):
        assert verdict(pol, call(tool, {"path": str(protected)}), (str(protected),)) == "deny", tool
        assert verdict(pol, call(tool, {"destination": str(box / "free")}), (str(protected),)) == "allow", tool
    assert verdict(pol, call("mcp__fs__read_file", {"path": str(protected)}), (str(protected),)) == "allow"
    assert (
        verdict(pol, call("mcp__fs__move", {"source": "a", "target": str(protected / "x")}), (str(protected),))
        == "deny"
    )


def test_arguments_past_the_depth_or_count_are_a_deny(box):
    pol = policy_of('allow_mcp = ["mcp__*"]')
    deep: dict = {"v": 1}
    for _ in range(gate.MCP_MAX_DEPTH + 1):
        deep = {"n": deep}
    assert verdict(pol, call("mcp__x__y", deep)) == "deny"
    shallow: dict = {"v": 1}
    for _ in range(gate.MCP_MAX_DEPTH - 2):
        shallow = {"n": shallow}
    assert verdict(pol, call("mcp__x__y", shallow)) == "allow"
    assert verdict(pol, call("mcp__x__y", {"items": list(range(gate.MCP_MAX_VALUES + 1))})) == "deny"
    assert verdict(pol, call("mcp__x__y", {"source": "\n".join(["line"] * 300)})) == "deny"


@pytest.mark.parametrize("tool_input", [None, "text", ["a"], 5])
def test_an_mcp_payload_the_gate_cannot_read_is_a_deny(box, tool_input):
    payload = {"tool_name": "mcp__x__y", "tool_input": tool_input, "session_id": "s", "cwd": "/"}
    args = gate.hook_args("check", ["--runner", "claude-code", *paths.pin_of().args()])
    assert gate.run_check(args, json.dumps(payload))[0] == gate.DENY
    with pytest.raises(TypeError):
        gate.check("claude-code", json.dumps(payload), policy_of(), (), None)


# --- the policy file -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'deny_tools = ["mcp__"]',
        'deny_tools = ["mcp__x__"]',
        'deny_tools = ["mcp____y"]',
        'deny_tools = ["mcp__x y__z"]',
        'deny_tools = ["mcp__x__[y]"]',
        'deny_tools = ["mcp_x__y"]',
        'deny_tools = ["MCP__x__y"]',
        'deny_tools = ["telepathy"]',
        'allow_mcp = ["x__y"]',
        'allow_mcp = ["mcp__x__y", "mcp__"]',
        "allow_mcp = 3",
        'allow_mcp = [""]',
    ],
)
def test_a_malformed_mcp_entry_refuses_the_whole_policy(line):
    key = line.split(" =")[0]
    with pytest.raises(policy.PolicyError, match=key):
        policy.loads("[gate]\n" + line + '\ndeny_commands = ["x"]\n')


def test_a_good_policy_with_mcp_entries_loads_whole():
    pol = policy_of(
        'deny_tools = ["web_fetch", "mcp__a-b__c.d", "mcp__*__delete_*", "mcp__?__y"]\nallow_mcp = ["mcp__ws__*"]'
    )
    assert pol.gate.allow_mcp == ("mcp__ws__*",) and len(pol.gate.deny_tools) == 4
    assert policy_of().gate.allow_mcp is None and policy_of("allow_mcp = []").gate.allow_mcp == ()


# --- the runners ---------------------------------------------------------------------------------------------------


def test_the_claude_code_settings_run_the_hook_for_mcp_tools_and_deny_the_exact_names(box):
    pol = policy_of('deny_tools = ["web_fetch", "mcp__x__y", "mcp__x__*"]')
    files = claude_code.generate(pol, ["ws"], box / "out", box / "wshome" / "policy.toml", pin=paths.pin_of())
    conf = json.loads(files["settings.json"])
    (group,) = conf["hooks"]["PreToolUse"]
    assert group["matcher"] == "*" and group["hooks"][0]["onFailure"] == "block"  # every tool, MCP tools among them
    deny = conf["permissions"]["deny"]
    assert "WebFetch" in deny and "mcp__x__y" in deny and not any("*" in d and d.startswith("mcp__") for d in deny)


def test_opencode_writes_a_native_deny_for_a_literal_server_and_cannot_tell_mcp_tools_in_the_hook(box):
    pol = policy_of('deny_tools = ["web_fetch", "mcp__x__y", "mcp__x__*", "mcp__*__delete_*", "mcp__*"]')
    files = opencode.generate(pol, ["ws"], box / "out", box / "wshome" / "policy.toml", pin=paths.pin_of())
    permission = json.loads(files[opencode.CONFIG])["permission"]
    assert permission["webfetch"] == "deny" and permission["x_y"] == "deny" and permission["x_*"] == "deny"
    assert not [k for k in permission if k.startswith("*")]  # a wildcard server would also catch OpenCode's own tools
    # the shim sends the tool's name and arguments and nothing says which tools are MCP tools, so the hook leaves them
    assert opencode.parse({"tool": "x_y", "args": {"path": "/etc/hostname"}}) is None


# --- the CLI path --------------------------------------------------------------------------------------------------


def test_replay_judges_mcp_calls_through_the_cli(box, capsys):
    folder = planted(box)
    pol_file = box / "wshome" / "policy.toml"
    pol_file.write_text(
        f'[gate]\ndeny_read = ["{folder}/**"]\nallow_mcp = ["mcp__ok__*"]\ndeny_tools = ["mcp__ok__drop"]\n'
    )
    rows = [
        {"payload": call("mcp__unknown__tool"), "expect": "ask"},
        {"payload": call("mcp__ok__fetch", {"n": 1}), "expect": "allow"},
        {"payload": call("mcp__ok__drop"), "expect": "deny"},
        {"payload": call("mcp__ok__fetch", {"file": str(folder / "f")}), "expect": "deny"},
        {"payload": call("mcp__ok__fetch", {"file": str(box / "free")}), "expect": "allow"},
        {"payload": {"tool_name": "mcp__ok__fetch", "tool_input": "oops"}, "expect": "deny"},
    ]
    file = box / "calls.jsonl"
    file.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code = cli.main(["gate", "replay", str(file), "--runner", "claude-code", "--policy", str(pol_file)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "2 allow, 3 deny, 1 ask" in out and "differ" not in out


def test_a_path_argument_too_long_to_read_is_denied(box):
    folder = planted(box)
    pol = sealed_policy(folder)
    long_path = str(folder) + "/" + "./" * 2100 + "f"
    assert verdict(pol, call("mcp__fs__read", {"path": long_path})) == "deny"
