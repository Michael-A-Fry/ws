"""The message wall: a walled or counts-only session's own send-message call is refused on every route, two walled
classes cannot message each other, and a full class or a folder in no named class is left alone. Every folder, class
and name here is made up under tmp_path."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ws import gate, jsonl, paths, walls
from ws import policy as policy_mod


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for d in ("home", "wshome", "house", "money", "open", "count", "plain"):
        (tmp_path / d).mkdir()
    text = (
        f'[classes.house]\naccess = "walled"\nrepos = ["{tmp_path}/house"]\n'
        f'[classes.money]\naccess = "walled"\nrepos = ["{tmp_path}/money"]\n'
        f'[classes.open]\naccess = "full"\nrepos = ["{tmp_path}/open"]\n'
        f'[classes.count]\naccess = "counts-only"\nrepos = ["{tmp_path}/count"]\n'
    )
    pol = tmp_path / "wshome" / "policy.toml"
    pol.write_text(text, encoding="utf-8")
    return tmp_path, str(pol)


def send(folder, to: str, text: str = "hello") -> dict:
    return {
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": text},
        "session_id": "s1",
        "cwd": str(folder),
        "hook_event_name": "PreToolUse",
    }


def run(payload: dict, pol: str) -> tuple[int, str, str]:
    """The hook's answer, for a session that started where the payload works unless a test set CLAUDE_PROJECT_DIR."""
    argv = ["--runner", "claude-code", "--policy", pol, *paths.pin_of().args()]
    set_here = "CLAUDE_PROJECT_DIR" not in os.environ
    if set_here:
        os.environ["CLAUDE_PROJECT_DIR"] = payload["cwd"]
    try:
        return gate.run_check(gate.hook_args("check", argv), json.dumps(payload))
    finally:
        if set_here:
            del os.environ["CLAUDE_PROJECT_DIR"]


@pytest.mark.parametrize(
    ("sender", "to"),
    [
        ("house", "money·house-numbers"),  # walled to walled, on this machine
        ("money", "house·house"),  # and back
        ("house", "house-numbers [3d472a]"),  # a Remote Control peer: the bridge route is the same call
        ("money", "lead-21nov"),  # even a lead's name: a walled session reaches its lead through `ws msg`
        ("count", "anyone"),
    ],
)
def test_a_walled_session_cannot_send_on_any_route(box, sender, to):
    tmp, pol = box
    code, _, err = run(send(tmp / sender, to), pol)
    assert code == 2
    assert "only through `ws msg`" in err and "SendMessage" in err


def test_a_full_class_may_send(box):
    tmp, pol = box
    assert run(send(tmp / "open", "anyone"), pol)[0] == 0


def test_a_walled_repos_worktree_outside_its_globs_is_unclassified_and_refused(box):
    tmp, pol = box
    tree = tmp / "house-wt" / "feature"  # a sibling worktree: no class names it, so it is the strictest case
    tree.mkdir(parents=True)
    code, _, err = run(send(tree, "money·x"), pol)
    assert code == 2 and "unclassified" in err
    assert run(send(tmp / "plain", "anyone"), pol)[0] == 2  # a new repo not yet in the policy, the same


def test_a_policy_with_no_classes_leaves_messages_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "wshome").mkdir()
    pol = tmp_path / "wshome" / "policy.toml"
    pol.write_text("", encoding="utf-8")
    assert run(send(tmp_path, "anyone"), str(pol))[0] == 0


def test_the_refusal_is_logged_in_the_class_copy_with_its_rule(box):
    tmp, pol = box
    run(send(tmp / "house", "money·x"), pol)
    lines = jsonl.read(paths.state_dir() / "logs" / "classes" / "house" / gate.VERDICT_LOG)
    assert [(r["tool"], r["decision"], r["rule"]) for r in lines] == [("SendMessage", "deny", "rule:message_wall")]
    assert "money" not in json.dumps(lines)  # counts and names only: no target, no text


def test_message_wall_judges_only_message_calls():
    walled = policy_mod.Class("w", "walled")
    assert gate.message_wall(walled, []) is None
    assert gate.message_wall(policy_mod.UNCLASSIFIED, [gate.Call("message", "x")])
    assert gate.message_wall(None, [gate.Call("message", "x")]) is None
    assert gate.message_wall(policy_mod.Class("f", "full"), [gate.Call("message", "x")]) is None
    assert gate.message_wall(walled, [gate.Call("message", "x")])


def test_the_walls_case_flags_a_crossing_that_got_through(box, monkeypatch):
    tmp, pol_path = box
    monkeypatch.setattr(walls, "CROSSINGS_SINCE", datetime(2000, 1, 1, tzinfo=UTC))  # the test's own clock is now
    pol = policy_mod.load(Path(pol_path))
    ctx = walls.Ctx(pol=pol, kind="linux", env=dict(os.environ), now=datetime.now(UTC))
    case = next(c for c in walls.CASES if c.name == "no-message-crossings")
    assert not case.host
    assert case.check(ctx).ok  # nothing logged yet
    run(send(tmp / "house", "money·x"), pol_path)  # refused: an attempt, not a crossing
    got = case.check(ctx)
    assert got.ok and "1 refused" in got.detail
    plant = paths.state_dir() / "logs" / "classes" / "money" / gate.VERDICT_LOG
    jsonl.append(plant, jsonl.stamped({"tool": "SendMessage", "decision": "allow", "session": "s9"}, 1))
    got = case.check(ctx)
    assert not got.ok and "money" in got.detail


def test_a_crossing_from_before_the_wall_shipped_is_history(box):
    _, pol_path = box
    pol = policy_mod.load(Path(pol_path))
    ctx = walls.Ctx(pol=pol, kind="linux", env=dict(os.environ), now=datetime.now(UTC))
    case = next(c for c in walls.CASES if c.name == "no-message-crossings")
    plant = paths.state_dir() / "logs" / "classes" / "money" / gate.VERDICT_LOG
    before = (walls.CROSSINGS_SINCE - timedelta(days=1)).timestamp()
    jsonl.append(plant, jsonl.stamped({"tool": "SendMessage", "decision": "allow", "session": "old"}, 1, before))
    assert case.check(ctx).ok


def test_a_session_started_unclassified_cannot_send_from_a_full_folder(box, monkeypatch):
    tmp, pol = box
    tree = tmp / "house-wt" / "feature"  # started in a walled repo's sibling worktree, no class names it
    tree.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tree))
    code, _, err = run(send(tmp / "open", "anyone"), pol)  # then working in a full class's folder
    assert code == 2 and "unclassified" in err
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp / "open"))  # started there: free to send
    assert run(send(tmp / "open", "anyone"), pol)[0] == 0
