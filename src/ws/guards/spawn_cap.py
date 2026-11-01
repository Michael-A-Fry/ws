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

`max_agents` has no default: the guard needs it, and while it's missing every subagent start is denied with a reason that
says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from ws.guards import Finding, known_keys
from ws.policy import PolicyError

NAME = "spawn-cap"
EVENTS = ("pre", "post", "subagent-stop")
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
        if cfg.max_agents is None or not _background(ctx):
            return None

        def started(state: dict) -> None:
            state["running"] = [*_fresh(state.get("running", []), now, cfg.stale_s), now]

        ctx.state(STATE, started)
        return None
    if cfg.max_agents is None:
        return Finding(
            "deny", "spawn-cap is on but [guards.spawn-cap] has no max_agents: set it to the most agents at once"
        )
    running = _fresh(ctx.state(STATE).get("running", []), now, cfg.stale_s)
    if len(running) >= cfg.max_agents:
        return Finding(
            "deny",
            f"{len(running)} agents are already running in this session (limit {cfg.max_agents}). Wait for one to "
            "finish, then start this one. The count is an estimate from the starts and stops ws has seen; the "
            f"runner's own task list shows what is running, and a start counts for {cfg.stale_s} seconds at most.",
        )
    return None
