"""spawn-cap: a session runs at most `max_agents` subagents at once. Before a subagent starts, the agents this session
has started and not seen stop are counted; at `max_agents` the call is denied and the reason says how to see what is
running. The check before the call only reads the count. A start is recorded after the call, once the runner has
let it through (so a call that was denied, here or by anything else, or that a person declined, uses no slot), and
when a subagent stops, one start is cleared.

The count is a heuristic. A start is recorded for an agent run in the background (`run_in_background`), the one whose
call returns while the agent still runs; a foreground agent has stopped by the time its call returns, so it is never
counted. A stop clears the oldest start, because a stop doesn't say which agent it was. A start older than `stale_s`
seconds (default 1800) is dropped, so an agent whose stop was never seen doesn't hold a slot for ever. Agents started
together in one message are all checked before any is recorded, so the cap can be passed by that many. The record is
the session's own, at `$WS_HOME/sessions/ID.spawn-cap.json`, and is advice: the sandbox is the wall.

`max_agents` is optional. When the policy gives none, the limit comes from the machine: the smaller of half the CPUs
and half the memory in GiB, raised to at least 2 and held to at most 8. The CPUs and memory are those the container or
service is allowed when the cgroup v2 files say so (`cpu.max` quota over period; `memory.max`), else `os.cpu_count()`
and no memory limit. A file that is missing, says `max` or can't be read counts as no limit. An explicit `max_agents`
always wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ws.guards import Finding, known_keys
from ws.policy import PolicyError

NAME = "spawn-cap"
EVENTS = ("pre", "post", "subagent-stop")
KINDS = ("agent",)

DEFAULT_STALE_S = 1800
STATE = "spawn-cap"
CGROUP_DIR = Path("/sys/fs/cgroup")  # cgroup v2 root; tests plant their own
FLOOR, CEILING = 2, 8


@dataclass(frozen=True)
class Config:
    max_agents: int | None = None  # None: work it out from the machine (default_max_agents)
    stale_s: int = DEFAULT_STALE_S


def _whole(table: dict, key: str, where: str, minimum: int, default: int | None) -> int | None:
    v = table.get(key, default)
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise PolicyError(f"{where}: {key} must be a whole number, {minimum} or more")
    return v


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("max_agents", "stale_s"))
    return Config(_whole(table, "max_agents", where, 1, None), _whole(table, "stale_s", where, 1, DEFAULT_STALE_S))


def _cgroup_value(cgroup_dir: Path, name: str) -> list[str]:
    try:
        return (cgroup_dir / name).read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError):
        return []


def default_max_agents(cgroup_dir: Path | None = None, cpu_count: int | None = None) -> int:
    """The limit when the policy gives none: min(CPUs // 2, GiB of memory // 2), at least 2 and at most 8. CPUs are
    the cgroup v2 `cpu.max` quota over its period when one is set, else the machine's; memory is `memory.max` when
    set, else not a limit."""
    root = CGROUP_DIR if cgroup_dir is None else cgroup_dir
    cpus = (os.cpu_count() or 1) if cpu_count is None else cpu_count
    quota = _cgroup_value(root, "cpu.max")
    if len(quota) == 2 and quota[0].isdecimal() and quota[1].isdecimal() and int(quota[1]) > 0:
        cpus = min(cpus, int(quota[0]) / int(quota[1]))
    limit = cpus // 2
    memory = _cgroup_value(root, "memory.max")
    if len(memory) == 1 and memory[0].isdecimal():
        limit = min(limit, int(memory[0]) // (2 * 1024**3))
    return int(max(FLOOR, min(CEILING, limit)))


def _fresh(running: list, now: float, stale_s: int) -> list[float]:
    return [t for t in running if isinstance(t, (int, float)) and not isinstance(t, bool) and now - t < stale_s]


def _background(ctx) -> bool:
    """True when the payload's tool input asks for the agent to run in the background."""
    tool_input = ctx.payload.get("tool_input")
    return isinstance(tool_input, dict) and tool_input.get("run_in_background") is True


def judge(call, cfg: Config, ctx) -> Finding | None:
    now = ctx.now()
    if ctx.event == "subagent-stop":

        def stopped(state: dict) -> None:
            state["running"] = _fresh(state.get("running", []), now, cfg.stale_s)[1:]

        ctx.state(STATE, stopped)
        return None
    if ctx.event == "post":
        if not _background(ctx):
            return None

        def started(state: dict) -> None:
            state["running"] = [*_fresh(state.get("running", []), now, cfg.stale_s), now]

        ctx.state(STATE, started)
        return None
    limit = cfg.max_agents if cfg.max_agents is not None else default_max_agents()
    running = _fresh(ctx.state(STATE).get("running", []), now, cfg.stale_s)
    if len(running) >= limit:
        return Finding(
            "deny",
            f"{len(running)} agents are already running in this session (limit {limit}). Wait for one to "
            "finish, then start this one. The count is an estimate from the starts and stops ws has seen; the "
            f"runner's own task list shows what is running, and a start counts for {cfg.stale_s} seconds at most.",
        )
    return None
