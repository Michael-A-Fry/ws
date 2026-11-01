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
and no memory limit. The files read are those of the session's own cgroup and the ones above it, the tightest limit
winning: `/proc/self/cgroup` names the cgroup, and `/sys/fs/cgroup` is read under it. Inside a cgroup namespace (a
container, or a service that has one) that file names the namespace's root, so `/sys/fs/cgroup` itself is the cgroup
and the limit is the container's or service's; on a host without one, the path in it finds the service's folder. A file
that is missing, says `max`, holds a number too big to use or can't be read counts as no limit. An explicit
`max_agents` always wins.
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
PROC_CGROUP = Path("/proc/self/cgroup")  # names this process's cgroup under that root
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


def _whole_number(text: str) -> int | None:
    """The number in a cgroup file's word, or None when it isn't one this code can use (`max`, junk, a huge value)."""
    try:
        return int(text) if text.isdecimal() else None
    except ValueError:  # more digits than int() will read
        return None


def _hierarchy(root: Path, proc_cgroup: Path) -> list[Path]:
    """The cgroup folders that bound this process, its own first and `root` last. The cgroup v2 line of
    `proc_cgroup` (`0::/path`) gives the path under `root`; without one, or when that folder isn't there, only `root`."""
    try:
        lines = proc_cgroup.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [root]
    rel = next((x[3:] for x in lines if x.startswith("0::")), "").removesuffix(" (deleted)")
    parts = [x for x in rel.split("/") if x and x != "."]
    if ".." in parts or not (root.joinpath(*parts)).is_dir():
        return [root]
    return [root.joinpath(*parts[:n]) for n in range(len(parts), -1, -1)]


def default_max_agents(
    cgroup_dir: Path | None = None, cpu_count: int | None = None, proc_cgroup: Path | None = None
) -> int:
    """The limit when the policy gives none: min(CPUs // 2, GiB of memory // 2), at least 2 and at most 8. CPUs are
    the cgroup v2 `cpu.max` quota over its period when one is set, else the machine's; memory is `memory.max` when
    set, else not a limit. With no `cgroup_dir` the process's own cgroup (and those above it) is found from
    `proc_cgroup` under /sys/fs/cgroup; a `cgroup_dir` given alone is read as it is."""
    root = CGROUP_DIR if cgroup_dir is None else cgroup_dir
    folders = (
        _hierarchy(root, PROC_CGROUP if proc_cgroup is None else proc_cgroup)
        if (cgroup_dir is None or proc_cgroup is not None)
        else [root]
    )
    cpus: float = (os.cpu_count() or 1) if cpu_count is None else cpu_count
    memory_limit: int | None = None
    for folder in folders:
        quota = _cgroup_value(folder, "cpu.max")
        if len(quota) == 2:
            top, period = _whole_number(quota[0]), _whole_number(quota[1])
            if top is not None and period:
                try:
                    cpus = min(cpus, top / period)
                except OverflowError:  # a quota too big to divide is no limit
                    pass
        memory = _cgroup_value(folder, "memory.max")
        if len(memory) == 1 and (m := _whole_number(memory[0])) is not None:
            memory_limit = m if memory_limit is None else min(memory_limit, m)
    limit = cpus // 2
    if memory_limit is not None:
        limit = min(limit, memory_limit // (2 * 1024**3))
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
