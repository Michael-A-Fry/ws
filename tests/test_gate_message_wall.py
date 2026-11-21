"""The message wall: a walled or counts-only session's own send-message call is refused on every route, two walled
classes cannot message each other, and a full class or a folder in no named class is left alone. Every folder, class
and name here is made up under tmp_path."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ws import gate, jsonl, paths, walls
from ws import policy as policy_mod


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
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
    argv = ["--runner", "claude-code", "--policy", pol, *paths.pin_of().args()]
    return gate.run_check(gate.hook_args("check", argv), json.dumps(payload))


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


@pytest.mark.parametrize("folder", ["open", "plain"])
def test_a_full_class_or_no_named_class_may_send(box, folder):
    tmp, pol = box
    assert run(send(tmp / folder, "anyone"), pol)[0] == 0


def test_the_refusal_is_logged_in_the_class_copy_with_its_rule(box):
    tmp, pol = box
    run(send(tmp / "house", "money·x"), pol)
    lines = jsonl.read(paths.state_dir() / "logs" / "classes" / "house" / gate.VERDICT_LOG)
    assert [(r["tool"], r["decision"], r["rule"]) for r in lines] == [("SendMessage", "deny", "rule:message_wall")]
    assert "money" not in json.dumps(lines)  # counts and names only: no target, no text


def test_message_wall_judges_only_message_calls():
    walled = policy_mod.Class("w", "walled")
    assert gate.message_wall(walled, []) is None
    assert gate.message_wall(policy_mod.UNCLASSIFIED, [gate.Call("message", "x")]) is None
    assert gate.message_wall(walled, [gate.Call("message", "x")])


def test_the_walls_case_flags_a_crossing_that_got_through(box):
    tmp, pol_path = box
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
