"""The spawn-cap guard (ws.guards.spawn_cap): the count of running agents, the stale limit, and per-session state."""

from __future__ import annotations

import json

import pytest

from ws import gate, guards, policy
from ws.adapters import claude_code
from ws.guards import Context
from ws.guards import spawn_cap as sc


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def start(cfg, home, clock, session="s1"):
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "worker", "prompt": "go"}}
    ctx = Context("claude-code", "pre", session, str(home), payload, clock, home / "wshome")
    return sc.judge(claude_code.parse(payload), cfg, ctx)


def stop(cfg, home, clock, session="s1"):
    ctx = Context("claude-code", "subagent-stop", session, str(home), {}, clock, home / "wshome")
    return sc.judge(None, cfg, ctx)


def cfg_of(**table):
    return sc.config(table, "[guards.spawn-cap]")


def test_it_runs_before_an_agent_starts_and_when_one_stops():
    mod = guards.get("spawn-cap")
    assert (mod.EVENTS, mod.KINDS) == (("pre", "subagent-stop"), ("agent",))


def test_it_denies_at_the_limit_and_says_how_to_see_what_is_running(home):
    cfg, clock = cfg_of(max_agents=2), Clock()
    assert start(cfg, home, clock) is None and start(cfg, home, clock) is None
    f = start(cfg, home, clock)
    assert f.action == "deny" and not f.approvable
    assert "2 agents are already running" in f.reason and "limit 2" in f.reason
    assert "task list" in f.reason and "estimate" in f.reason and "1800 seconds" in f.reason


def test_a_denied_start_is_not_counted(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    assert start(cfg, home, clock) is None
    for _ in range(3):
        assert start(cfg, home, clock).action == "deny"
    assert stop(cfg, home, clock) is None
    assert start(cfg, home, clock) is None  # one stop freed exactly one slot
    assert start(cfg, home, clock).action == "deny"


def test_a_stop_with_nothing_running_does_not_go_below_zero(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    stop(cfg, home, clock)
    stop(cfg, home, clock)
    assert start(cfg, home, clock) is None
    assert start(cfg, home, clock).action == "deny"


def test_a_start_older_than_stale_s_is_dropped(home):
    cfg, clock = cfg_of(max_agents=1, stale_s=60), Clock()
    assert start(cfg, home, clock) is None
    clock.t += 59
    assert start(cfg, home, clock).action == "deny"
    clock.t += 2  # 61 seconds after the first start
    assert start(cfg, home, clock) is None


def test_the_default_stale_limit_is_half_an_hour(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    start(cfg, home, clock)
    clock.t += 1799
    assert start(cfg, home, clock).action == "deny"
    clock.t += 2
    assert start(cfg, home, clock) is None


def test_the_count_is_kept_across_calls_and_for_each_session(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    assert start(cfg, home, clock, "a") is None
    assert start(cfg, home, clock, "a").action == "deny"
    assert start(cfg, home, clock, "b") is None  # another session has its own count
    stop(cfg, home, clock, "b")
    assert start(cfg, home, clock, "a").action == "deny"  # b's stop did not free a slot for a
    record = json.loads((home / "wshome" / "sessions" / "a.spawn-cap.json").read_text())
    assert len(record["running"]) == 1


def test_a_stop_clears_the_oldest_start(home):
    cfg, clock = cfg_of(max_agents=3, stale_s=100), Clock()
    start(cfg, home, clock)  # t=1000
    clock.t += 50
    start(cfg, home, clock)  # t=1050
    stop(cfg, home, clock)  # clears 1000
    clock.t += 60  # 1110: 1050 is 60 old, still live
    start(cfg, home, clock)
    start(cfg, home, clock)
    assert start(cfg, home, clock).action == "deny"


def test_with_no_limit_set_every_start_is_denied_and_says_why(home):
    f = start(cfg_of(), home, Clock())
    assert f.action == "deny" and "max_agents" in f.reason


@pytest.mark.parametrize(
    "table, key",
    [
        ({"max_agents": 0}, "max_agents"),
        ({"max_agents": "4"}, "max_agents"),
        ({"max_agents": True}, "max_agents"),
        ({"max_agents": 2.5}, "max_agents"),
        ({"max_agents": 2, "stale_s": 0}, "stale_s"),
        ({"max_agents": 2, "stale": 5}, "stale"),
    ],
)
def test_a_bad_table_names_the_key(table, key):
    with pytest.raises(policy.PolicyError, match=key):
        sc.config(table, "[guards.spawn-cap]")


def test_the_policy_loads_the_table_and_the_gate_denies_the_fifth_agent(home):
    pol = policy.loads('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 4\nstale_s = 600\n')
    assert pol.guards.config["spawn-cap"] == sc.Config(4, 600)
    raw = json.dumps(
        {
            "tool_name": "Agent",
            "session_id": "s9",
            "cwd": str(home),
            "tool_input": {"subagent_type": "w", "prompt": "p"},
        }
    )
    for _ in range(4):
        assert gate.check("claude-code", raw, pol, home=home / "wshome") is None
    assert gate.check("claude-code", raw, pol, home=home / "wshome").startswith(
        "spawn-cap: 4 agents are already running"
    )


def test_a_subagent_stop_hook_runs_for_the_guard(home):
    pol = policy.loads('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 1\n')
    files = claude_code.generate(pol, ["ws"], home / "out", home / "p.toml")
    hooks = json.loads(files["settings.json"])["hooks"]
    assert "SubagentStop" in hooks and "Agent|Task" in hooks["PreToolUse"][0]["matcher"]
    start_raw = json.dumps({"tool_name": "Agent", "session_id": "s", "cwd": str(home), "tool_input": {"prompt": "p"}})
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome") is None
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome")
    code, _, _ = gate.event(
        "claude-code",
        json.dumps({"hook_event_name": "SubagentStop", "session_id": "s", "cwd": str(home)}),
        pol,
        home=home / "wshome",
    )
    assert code == 0
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome") is None
