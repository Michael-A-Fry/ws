"""spawn-cap: a session runs at most `max_agents` subagents at once. Before a subagent starts, the agents this session
has started and not seen stop are counted; at `max_agents` the call is denied and the reason says how to see what is
running. When a subagent stops, one start is cleared.

The count is a heuristic. A start is recorded when the call is made, and a stop clears the oldest start, because a stop
doesn't say which agent it was. A start older than `stale_s` seconds (default 1800) is dropped, so an agent whose stop
was never seen doesn't hold a slot for ever. The record is the session's own, at `$WS_HOME/sessions/ID.spawn-cap.json`.

`max_agents` has no default: the guard needs it, and while it's missing every subagent start is denied with a reason that
says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from ws.guards import Finding, known_keys
from ws.policy import PolicyError

NAME = "spawn-cap"
EVENTS = ("pre", "subagent-stop")
KINDS = ("agent",)

DEFAULT_STALE_S = 1800
STATE = "spawn-cap"


@dataclass(frozen=True)
class Config:
    max_agents: int | None = None  # None: the policy names the guard but gives no limit
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


def _fresh(running: list, now: float, stale_s: int) -> list[float]:
    return [t for t in running if isinstance(t, (int, float)) and not isinstance(t, bool) and now - t < stale_s]


def judge(call, cfg: Config, ctx) -> Finding | None:
    now = ctx.now()
    if ctx.event == "subagent-stop":

        def stopped(state: dict) -> None:
            state["running"] = _fresh(state.get("running", []), now, cfg.stale_s)[1:]

        ctx.state(STATE, stopped)
        return None
    if cfg.max_agents is None:
        return Finding(
            "deny", "spawn-cap is on but [guards.spawn-cap] has no max_agents: set it to the most agents at once"
        )
    refused: list[int] = []

    def started(state: dict) -> None:
        running = _fresh(state.get("running", []), now, cfg.stale_s)
        if len(running) >= cfg.max_agents:
            refused.append(len(running))
        else:
            running.append(now)
        state["running"] = running

    ctx.state(STATE, started)
    if refused:
        return Finding(
            "deny",
            f"{refused[0]} agents are already running in this session (limit {cfg.max_agents}). Wait for one to "
            "finish, then start this one. The count is an estimate from the starts and stops ws has seen; the "
            f"runner's own task list shows what is running, and a start counts for {cfg.stale_s} seconds at most.",
        )
    return None
