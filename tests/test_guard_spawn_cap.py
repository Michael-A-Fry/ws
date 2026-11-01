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


def _ctx(event, home, clock, session, payload):
    return Context("claude-code", event, session, str(home), payload, clock, home / "wshome")


def pre(cfg, home, clock, session="s1", background=True):
    """The check before an Agent call; it only reads the count."""
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "worker", "prompt": "go", "run_in_background": background},
    }
    return sc.judge(claude_code.parse(payload), cfg, _ctx("pre", home, clock, session, payload))


def post(cfg, home, clock, session="s1", background=True):
    """The hook after an Agent call that went through."""
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "worker", "prompt": "go", "run_in_background": background},
    }
    return sc.judge(claude_code.parse(payload), cfg, _ctx("post", home, clock, session, payload))


def start(cfg, home, clock, session="s1"):
    """An Agent call as the runner makes it: the check, and when that lets it through, the hook after it."""
    found = pre(cfg, home, clock, session)
    if found is None:
        post(cfg, home, clock, session)
    return found


def stop(cfg, home, clock, session="s1"):
    ctx = Context("claude-code", "subagent-stop", session, str(home), {}, clock, home / "wshome")
    return sc.judge(None, cfg, ctx)


def cfg_of(**table):
    return sc.config(table, "[guards.spawn-cap]")


def test_it_runs_before_and_after_an_agent_starts_and_when_one_stops():
    mod = guards.get("spawn-cap")
    assert (mod.EVENTS, mod.KINDS) == (("pre", "post", "subagent-stop"), ("agent",))


def test_the_check_before_a_call_uses_no_slot(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    for _ in range(5):  # calls that were declined, or denied by another guard, never reach the hook after them
        assert pre(cfg, home, clock) is None
    assert start(cfg, home, clock) is None
    assert pre(cfg, home, clock).action == "deny"


def test_a_denied_or_declined_agent_call_leaves_no_slot_used(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    assert start(cfg, home, clock) is None
    assert pre(cfg, home, clock).action == "deny"  # denied: no hook after it
    assert stop(cfg, home, clock) is None
    assert pre(cfg, home, clock) is None  # a person declines this one: no hook after it
    assert pre(cfg, home, clock) is None  # so the slot is still free
    assert start(cfg, home, clock) is None


def test_the_hook_after_a_call_records_the_start(home):
    cfg, clock = cfg_of(max_agents=2), Clock()
    post(cfg, home, clock)
    post(cfg, home, clock)
    assert pre(cfg, home, clock).action == "deny"
    record = json.loads((home / "wshome" / "sessions" / "s1.spawn-cap.json").read_text())
    assert record["running"] == [1000.0, 1000.0]


def test_a_foreground_agent_has_stopped_before_its_call_returns_so_it_is_not_counted(home):
    cfg, clock = cfg_of(max_agents=1), Clock()
    post(cfg, home, clock, background=False)
    assert pre(cfg, home, clock) is None


def test_the_hook_after_a_call_with_no_limit_set_records_the_start(home, cgroup):
    cgroup(cpu="800000 100000")  # 8 CPUs: the machine's default is 4
    cfg, clock = cfg_of(), Clock()
    for _ in range(4):
        post(cfg, home, clock)
    assert pre(cfg, home, clock).action == "deny"


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


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
    """Plant a cgroup v2 folder and point the guard at it; the real /sys is never read."""
    root = tmp_path / "cgroup"
    root.mkdir()
    monkeypatch.setattr(sc, "CGROUP_DIR", root)
    monkeypatch.setattr(sc, "PROC_CGROUP", tmp_path / "no-proc-cgroup")  # the real /proc is never read either
    monkeypatch.setattr(sc.os, "cpu_count", lambda: 16)

    def plant(cpu=None, memory=None):
        for name, text in (("cpu.max", cpu), ("memory.max", memory)):
            if text is not None:
                (root / name).write_text(text + "\n")
        return root

    return plant


GIB = 1024**3


@pytest.mark.parametrize(
    "cpu, memory, expected",
    [
        (None, None, 8),  # no cgroup files: 16 CPUs // 2 = 8, no memory limit
        ("max 100000", "max", 8),  # files that say "no limit"
        ("400000 100000", None, 2),  # 4 CPUs of quota // 2
        ("1000000 100000", None, 5),  # 10 CPUs of quota // 2
        ("50000 100000", None, 2),  # half a CPU: raised to the floor
        ("max 100000", str(8 * GIB), 4),  # 8 GiB // 2 beats 16 CPUs // 2
        ("1600000 100000", str(64 * GIB), 8),  # held to the ceiling
        ("200000 100000", str(2 * GIB), 2),  # both low: the floor
        ("junk", "junk", 8),  # unreadable values count as no limit
        ("100 0", None, 8),  # a zero period is no quota
        ("1000 -5", "-1", 8),
    ],
)
def test_the_default_limit_comes_from_the_cgroup_quota_and_memory(cgroup, cpu, memory, expected):
    cgroup(cpu, memory)
    assert sc.default_max_agents() == expected


def test_the_default_limit_reads_the_folder_it_is_given_and_the_cpu_count_it_is_told(tmp_path):
    (tmp_path / "memory.max").write_text(str(12 * GIB))
    assert sc.default_max_agents(tmp_path, cpu_count=32) == 6
    assert sc.default_max_agents(tmp_path / "missing", cpu_count=6) == 3


def test_a_number_too_big_to_read_is_no_limit(cgroup):
    huge = "9" * 5000
    cgroup(f"{huge} 100000", huge)
    assert sc.default_max_agents() == 8
    cgroup(f"100000 {huge}", huge)
    assert sc.default_max_agents() == 8


def _service_tree(tmp_path, own_cpu=None, own_memory=None, parent_cpu=None, proc="0::/system.slice/ws.service\n"):
    """A host's cgroup tree with the session in system.slice/ws.service, and the /proc/self/cgroup file for it."""
    root = tmp_path / "host-cgroup"
    own = root / "system.slice" / "ws.service"
    own.mkdir(parents=True)
    for folder, name, text in (
        (own, "cpu.max", own_cpu),
        (own, "memory.max", own_memory),
        (root / "system.slice", "cpu.max", parent_cpu),
    ):
        if text is not None:
            (folder / name).write_text(text + "\n")
    (tmp_path / "proc-cgroup").write_text(proc)
    return root, tmp_path / "proc-cgroup"


def test_on_a_host_the_limit_is_read_from_the_cgroup_the_session_is_in(tmp_path):
    root, proc = _service_tree(tmp_path, own_cpu="400000 100000", own_memory=str(64 * GIB))
    (root / "cpu.max").write_text("max 100000\n")
    assert sc.default_max_agents(root, cpu_count=32, proc_cgroup=proc) == 2  # the service's 4 CPUs, not the host's 32
    assert sc.default_max_agents(root, cpu_count=32) == 8  # the root alone is read when no /proc file is named


def test_a_limit_on_a_folder_above_the_service_counts_and_the_tightest_wins(tmp_path):
    root, proc = _service_tree(tmp_path, own_cpu="max 100000", parent_cpu="1200000 100000")
    assert sc.default_max_agents(root, cpu_count=32, proc_cgroup=proc) == 6
    root, proc = _service_tree(tmp_path / "b", own_cpu="400000 100000", parent_cpu="1200000 100000")
    assert sc.default_max_agents(root, cpu_count=32, proc_cgroup=proc) == 2


@pytest.mark.parametrize(
    "proc",
    [
        "0::/\n",
        "",
        "1:cpu:/system.slice\n",
        "0::/missing/dir\n",
        "0::/../../etc\n",
        "0::/system.slice/ws.service (deleted)\n",
    ],
)
def test_a_cgroup_namespace_root_or_an_unusable_proc_file_reads_the_root(tmp_path, proc):
    root, path = _service_tree(tmp_path, own_cpu="200000 100000", proc=proc)
    (root / "cpu.max").write_text("800000 100000\n")
    # "(deleted)" is still the service's folder; every other line here leaves the root's 8 CPUs, 4 agents
    assert sc.default_max_agents(root, cpu_count=32, proc_cgroup=path) == (2 if "(deleted)" in proc else 4)
    assert sc.default_max_agents(root, cpu_count=32, proc_cgroup=tmp_path / "gone") == 4


def test_a_cpu_quota_above_the_machine_s_cpus_does_not_raise_the_limit(cgroup):
    cgroup("6400000 100000")  # a quota of 64 CPUs on a 16 CPU machine
    assert sc.default_max_agents() == 8
    assert sc.default_max_agents(cpu_count=6, cgroup_dir=sc.CGROUP_DIR) == 3


def test_with_no_limit_set_the_machine_s_default_applies_and_the_reason_names_it(home, cgroup):
    cgroup(cpu="200000 100000")  # 2 CPUs: the floor of 2
    cfg, clock = cfg_of(), Clock()
    assert start(cfg, home, clock) is None and start(cfg, home, clock) is None
    f = start(cfg, home, clock)
    assert f.action == "deny" and "limit 2" in f.reason


def test_an_explicit_max_agents_wins_over_the_machine_default(home, cgroup):
    cgroup(cpu="200000 100000", memory=str(GIB))  # the default would be 2
    cfg, clock = cfg_of(max_agents=5), Clock()
    for _ in range(5):
        assert start(cfg, home, clock) is None
    assert start(cfg, home, clock).action == "deny"
    assert sc.config({"max_agents": 1}, "x").max_agents == 1 and sc.config({}, "x").max_agents is None


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
            "tool_input": {"subagent_type": "w", "prompt": "p", "run_in_background": True},
        }
    )
    post_raw = json.dumps({**json.loads(raw), "hook_event_name": "PostToolUse"})
    for _ in range(4):
        assert gate.check("claude-code", raw, pol, home=home / "wshome") is None
        gate.event("claude-code", post_raw, pol, home=home / "wshome")
    assert gate.check("claude-code", raw, pol, home=home / "wshome").startswith(
        "spawn-cap: 4 agents are already running"
    )


def test_a_subagent_stop_hook_runs_for_the_guard(home):
    pol = policy.loads('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 1\n')
    files = claude_code.generate(pol, ["ws"], home / "out", home / "p.toml")
    hooks = json.loads(files["settings.json"])["hooks"]
    assert "SubagentStop" in hooks and "Agent|Task" in hooks["PreToolUse"][0]["matcher"]
    assert "Agent|Task" in hooks["PostToolUse"][0]["matcher"]
    start_raw = json.dumps(
        {
            "tool_name": "Agent",
            "session_id": "s",
            "cwd": str(home),
            "tool_input": {"prompt": "p", "run_in_background": True},
        }
    )
    post_raw = json.dumps({**json.loads(start_raw), "hook_event_name": "PostToolUse"})
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome") is None
    gate.event("claude-code", post_raw, pol, home=home / "wshome")
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome")
    code, _, _ = gate.event(
        "claude-code",
        json.dumps({"hook_event_name": "SubagentStop", "session_id": "s", "cwd": str(home)}),
        pol,
        home=home / "wshome",
    )
    assert code == 0
    assert gate.check("claude-code", start_raw, pol, home=home / "wshome") is None
