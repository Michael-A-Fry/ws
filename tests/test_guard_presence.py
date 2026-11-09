"""The presence guard: busy and idle from the hooks a session already runs, in the registry, newest report winning."""

from __future__ import annotations

import json

import pytest
from test_msg import World, world  # noqa: F401 - the fixture, imported so this file uses the same registry stand-ins

from ws import gate, guards, jsonl, policy, session
from ws.guards import presence

POL = policy.loads('[guards]\non = ["presence"]\n')
T0 = 1_700_000_000.0


class Clock:
    """The hook's clock and the registry's, moved together."""

    def __init__(self, mp):
        self.t = T0
        mp.setattr(session, "_now", lambda: self.t)

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def hook(name: str, sid: str = "alpha", **extra) -> str:
    return json.dumps({"hook_event_name": name, "session_id": sid, "cwd": "/w", **extra})


def tool(name: str, sid: str = "alpha", **extra) -> str:
    return hook(name, sid, tool_name="Bash", tool_input={"command": "ls"}, **extra)


def send(world, clock, at: float, name: str, **extra):  # noqa: F811
    clock.t = at
    raw = tool(name, **extra) if name in ("PreToolUse", "PostToolUse") else hook(name, **extra)
    if name == "PreToolUse":
        return gate.check("claude-code", raw, POL, home=world.root / "wshome", clock=clock)
    return gate.event("claude-code", raw, POL, home=world.root / "wshome", clock=clock)


def test_events_map_to_busy_and_idle(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    assert session.read("alpha").state is None
    steps = [
        ("SessionStart", "idle"),
        ("UserPromptSubmit", "busy"),
        ("PreToolUse", "busy"),
        ("PostToolUse", "busy"),
        ("Stop", "idle"),
        ("UserPromptSubmit", "busy"),
    ]
    for n, (name, expect) in enumerate(steps):
        send(world, clock, T0 + 100 * (n + 1), name)
        assert session.read("alpha").state == expect, name
    assert session.read("alpha").state_ts == jsonl.iso_utc(T0 + 600)
    assert presence.HOOK_STATE == {"start": "idle", "prompt": "busy", "pre": "busy", "post": "busy", "stop": "idle"}
    assert set(presence.EVENTS) <= set(guards.EVENTS)  # no event of its own: only the ones ws already has


def test_a_hook_never_fails_and_says_nothing(world, clock):  # noqa: F811
    # no record at all, and a payload with no session id: both silent, both allowed
    assert send(world, clock, T0, "PreToolUse") is None
    assert send(world, clock, T0 + 1, "Stop") == (0, "", "")
    raw = json.dumps({"hook_event_name": "Stop", "cwd": "/w"})
    assert gate.event("claude-code", raw, POL, home=world.root / "wshome", clock=clock) == (0, "", "")


def test_a_sub_agents_hook_is_not_the_sessions_state(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    send(world, clock, T0 + 10, "Stop")
    send(world, clock, T0 + 20, "PostToolUse", agent_id="sub-1")
    assert session.read("alpha").state == "idle"
    send(world, clock, T0 + 30, "SubagentStop")  # the guard has no row for it
    assert session.read("alpha").state == "idle"


def test_stale_state_ages_out(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    send(world, clock, T0, "UserPromptSubmit")
    rec = session.read("alpha")
    assert presence.STALE_S == 120
    assert presence.effective_state(rec, T0 + 60) == "busy"
    assert presence.effective_state(rec, T0 + 120) == "busy"
    assert presence.effective_state(rec, T0 + 121) is None  # busy that has gone quiet: can't tell, the strictest case
    assert presence.effective_state(rec, T0 - 5) is None  # a time from the future is no evidence either
    send(world, clock, T0 + 130, "Stop")
    assert presence.effective_state(session.read("alpha"), T0 + 86400) == "idle"  # a stopped session stays stopped
    assert presence.effective_state(None, T0) is None
    bad = session.Record.from_dict({"session_id": "x", "release": "1", "state": "busy", "state_ts": "not a time"})
    assert presence.effective_state(bad, T0) is None
    assert presence.effective_state(session.Record.from_dict({"session_id": "y", "release": "1"}), T0) is None


def test_out_of_order_events_keep_the_newer(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    send(world, clock, T0 + 200, "Stop")
    assert session.read("alpha").state == "idle"
    send(world, clock, T0 + 190, "PreToolUse")  # a slower hook, from before the stop, arriving after it
    assert session.read("alpha").state == "idle"
    send(world, clock, T0 + 195, "PostToolUse")
    assert session.read("alpha").state == "idle"
    send(world, clock, T0 + 210, "UserPromptSubmit")  # a newer one still gets through
    assert session.read("alpha").state == "busy"
    held = json.loads((world.root / "wshome" / "sessions" / "alpha.presence.json").read_text())
    assert held["source"] == "hook" and held["seq"] == int((T0 + 210) * 1e9)


def test_a_lower_authority_source_does_not_override_a_fresh_hook(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    send(world, clock, T0 + 10, "UserPromptSubmit")
    path = world.root / "wshome" / "sessions" / "alpha.presence.json"
    held = json.loads(path.read_text())
    held["source"] = "higher"  # a source that outranks a hook (a later one): fresh, so it holds
    path.write_text(json.dumps(held))
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setitem(presence.AUTHORITY, "higher", 3)
        send(world, clock, T0 + 20, "Stop")
        assert session.read("alpha").state == "busy"
        send(world, clock, T0 + 20 + presence.STALE_S + 1, "Stop")  # once it has gone stale a hook may speak again
        assert session.read("alpha").state == "idle"
    finally:
        monkey.undo()


def test_an_unchanged_state_is_not_written_again_at_once(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    send(world, clock, T0, "PreToolUse")
    rev = session.read("alpha").rev
    send(world, clock, T0 + 5, "PreToolUse")
    send(world, clock, T0 + 10, "PostToolUse")
    assert session.read("alpha").rev == rev
    send(world, clock, T0 + presence.REFRESH_S + 1, "PreToolUse")  # freshness is kept up, not left to go stale
    assert session.read("alpha").rev == rev + 1


def test_a_record_that_moves_between_the_read_and_the_write_is_read_again(world, clock, monkeypatch):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    real = session.update
    calls = []

    def racing(sid, **kw):
        calls.append(kw.get("expect_rev"))
        if len(calls) == 1:
            real(
                sid, last_delivered_outbound={"id": "9", "ts": "2026-01-01T00:00:00.000Z"}
            )  # someone else got in first
        return real(sid, **kw)

    monkeypatch.setattr(session, "update", racing)
    send(world, clock, T0, "UserPromptSubmit")
    assert len(calls) == 2 and calls[1] == calls[0] + 1
    assert session.read("alpha").state == "busy"


def test_the_last_delivered_outbound_is_kept_in_the_registry(world, clock):  # noqa: F811
    world.session("alpha", world.folder("alpha"))
    world.session("bravo", world.folder("bravo"))
    sent = world.send("alpha", "bravo", "done", None)
    from ws import msg

    world.who = "bravo"
    msg.mark_delivered(sent["id"], "bravo", now=T0 + 5)
    send(world, clock, T0 + 10, "Stop")
    got = session.read("alpha").last_delivered_outbound
    assert got == {"id": str(sent["id"]), "ts": jsonl.iso_utc(T0 + 5)}
