"""Guards: small, named checks the policy switches on, run by `ws gate` at the points a session can be watched.

A guard is a module in this folder (the directory is the registry, as for runner adapters; a name starting with `_` is
a helper and is not a guard). It defines:

    NAME: str                  the name used in `[guards] on = [...]` and as the table `[guards.NAME]`
    EVENTS: tuple[str, ...]    where it runs, a subset of "pre" (before a tool call), "post" (after one), "start"
                               (a session begins), "prompt" (a prompt is sent), "stop" (the session stops) and
                               "subagent-stop" (a subagent stops)
    KINDS: tuple[str, ...]     the Call kinds it reads on "pre" and "post" (see ws.adapters.Call); a call of any other
                               kind never reaches it. The other events carry no call
    PRIORITY: int              where it runs among the guards that are on: lowest first, then by name (default 50)
    def config(table: dict, where: str) -> object
                               validate its `[guards.NAME]` table (`{}` when the policy has none) and return what
                               `judge` receives as `cfg`; raise ws.policy.PolicyError naming the key and `where`
    def judge(call, cfg, ctx) -> Finding | None
                               look at one call (None on the events that carry none) and return a Finding, or None.
                               It may raise: before a call that is a deny (the gate fails closed); on every other
                               event it is one stderr line and exit 0

and, optionally:

    NATIVE_DENY: tuple[str, ...]   deny patterns for the runner's own rule list, added while the guard is on (the
                                   same glob syntax as `deny_commands`; the runner enforces them with no hook)

`Finding(action, reason, approvable)`: "deny" stops a call (before it) or tells the model what went wrong (after it);
"warn" and "note" are advice. An approvable deny can be overridden once by the person with `ws approve ID`, bound to the
runner, the call, the folder and the session, exactly as `ask_commands` is.

`Context` carries what a guard may know: the runner, the event, the session id, the folder, the raw payload, `now()`
(the clock, so a test can set it), `memo` (scratch shared by the guards for one hook run) and `state(name, update)`, a small JSON record of its own for this session, read and
written under a lock at `<state>/sessions/<session id>.<name>.json` and removed with the session record.

The order is fixed and the same for every runner and every call: the policy's own rules first (`deny_commands`,
`deny_read` and the rest, see ws.gate), then the guards that are on, lowest `PRIORITY` first and by name within a
priority, whatever order `[guards] on` lists them in. The first deny stops the run: a guard after it is not asked. The
bands in use are 0 for a guard that only records (so a refused call is recorded too), 10 for a guard that protects
the machine or the repository, 20 for one that keeps the session honest about its own work, 30 for one that checks what a
message says, and 90 for advice after the fact. `stream` and `run` take an optional list that gets one
`{guard, result, ms}` for each guard that ran, which `ws gate` writes to the verdict log (see docs/reference/stores.md).

Every guard takes `mode = "enforce"` (the default) or `mode = "log"` in its `[guards.NAME]` table, a key the policy
reads and `config()` never sees (see ws.policy). The effective mode is log only when log is asked for AND the sandbox
wall is proven for this call (`Context.wall`: the gate decides, from the platform, the sandbox binary on PATH, ws's own
settings marker and the adapter's preflight, and `wall_gap` says why not in one fixed word). Then `stream` turns the
guard's deny into a note and a `would-deny` verdict, so the call goes ahead and the log holds a count; an approvable deny
asks for no approval. Without the wall the guard refuses exactly as in enforce, and its verdict carries the word. A guard
asked to log gets no NATIVE_DENY in the runner's list: a runner's own rule can't be logged instead of refused.

A new guard is one module here; nothing else changes. The policy turns a guard on, and `ws gate generate` writes the
runner hooks for the events and tools the guards that are on need.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from ws.policy import PolicyError

EVENTS = ("pre", "post", "start", "prompt", "stop", "subagent-stop")
ACTIONS = ("deny", "warn", "note")


@dataclass(frozen=True)
class Finding:
    """What a guard found. action: "deny", "warn" or "note". approvable: a deny the person may override once."""

    action: str
    reason: str
    approvable: bool = False
    would_deny: bool = (
        False  # set by `stream` alone: a deny the guard's log mode let through (the action is then "note")
    )

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown finding action {self.action!r}")


@dataclass
class Context:
    """What a guard may know about the moment it runs. `home` is ws's state folder (default `paths.state_dir()`)."""

    runner: str
    event: str
    session_id: str = ""
    cwd: str = ""
    payload: dict = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    home: Path | None = None
    memo: dict = field(default_factory=dict)  # scratch for one hook run, shared by the guards (not kept)
    wall: Callable[[], str] | None = None  # "" when the sandbox wall is proven for this call, else why not; None = not

    def now(self) -> float:
        return self.clock()

    def wall_gap(self) -> str:
        """Why the wall is not proven for this call, as one of ws.gate.WALL_GAPS, or "" when it is. A context built with no
        proof (a test, a replay) has none."""
        return "sandbox-off" if self.wall is None else self.wall()

    def state(self, name: str, update: Callable[[dict], dict | None] | None = None) -> dict:
        """This guard's record for this session. With `update`, one locked read-modify-write: `update` gets the
        current dict (empty when there is none) and either changes it in place or returns the new one; the result is
        stored and returned. A record that cannot be read starts again empty."""
        if not self.session_id:
            raise ValueError("no session id, so no state")
        return _state(self.home, self.session_id, name, update)


def _state_path(home: Path | None, session_id: str, name: str) -> Path:
    from ws import paths
    from ws.session import safe_id

    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"{name!r} is not a plain state name")
    return (Path(home) if home is not None else paths.state_dir()) / "sessions" / f"{safe_id(session_id)}.{name}.json"


def _state(home: Path | None, session_id: str, name: str, update: Callable[[dict], dict | None] | None) -> dict:
    path = _state_path(home, session_id, name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        try:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)  # released when the file closes
        except ImportError:  # pragma: no cover - native Windows has no flock; the record is advice only
            pass
        try:
            data = json.loads(f.read() or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if update is not None:
            changed = update(data)
            data = data if changed is None else changed
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data))
    return data


# --- helpers for a guard's config() --------------------------------------------------------------------------------


def known_keys(table: Any, where: str, allowed: tuple[str, ...] = ()) -> dict:
    """`table` if it is a table with no key outside `allowed`; else a PolicyError naming the first stray key."""
    if not isinstance(table, dict):
        raise PolicyError(f"{where} must be a table")
    extra = sorted(set(table) - set(allowed))
    if extra:
        known = f" (known: {', '.join(sorted(allowed))})" if allowed else " (it takes no keys)"
        raise PolicyError(f"{where}: unknown key {extra[0]!r}{known}")
    return table


def text_list(table: dict, key: str, where: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """A list of non-empty text at `key`, or `default` when the key is absent."""
    v = table.get(key, list(default))
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise PolicyError(f"{where}: {key} must be a list of non-empty text")
    return tuple(v)


# --- the registry --------------------------------------------------------------------------------------------------


def all_guards() -> dict[str, ModuleType]:
    found = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, "NAME") and hasattr(mod, "judge"):
            found[mod.NAME] = mod
    return dict(sorted(found.items()))


def get(name: str) -> ModuleType:
    guards = all_guards()
    if name not in guards:
        raise KeyError(f"unknown guard {name!r} (known: {', '.join(guards)})")
    return guards[name]


def names() -> tuple[str, ...]:
    return tuple(all_guards())


DEFAULT_PRIORITY = 50


def priority(guard: ModuleType) -> int:
    """Where a guard runs among the guards that are on (`PRIORITY`, lowest first)."""
    value = getattr(guard, "PRIORITY", DEFAULT_PRIORITY)
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_PRIORITY


def wanted(on: tuple[str, ...], event: str) -> list[ModuleType]:
    """The guards that are on and run at `event`, in the one fixed order: lowest `PRIORITY` first, then by name. The
    order `[guards] on` lists them in does not matter."""
    chosen = [g for g in (get(n) for n in dict.fromkeys(on)) if event in g.EVENTS]
    return sorted(chosen, key=lambda g: (priority(g), g.NAME))


def kinds_for(on: tuple[str, ...], event: str) -> set[str]:
    """The Call kinds the guards that are on read at `event`."""
    return {k for g in wanted(on, event) for k in g.KINDS}


def native_deny(on: tuple[str, ...], modes: dict | None = None) -> list[str]:
    """The deny patterns for a runner's own rule list that the guards that are on contribute, but not those of a guard
    whose mode is log: the runner would refuse the call outright, where the guard's mode is to log it when the wall holds
    (and the hook, which still runs, refuses it where the wall is not proven)."""
    modes = modes or {}
    return list(
        dict.fromkeys(p for n in on if modes.get(n, "enforce") != "log" for p in getattr(get(n), "NATIVE_DENY", ()))
    )


def requested_log(pol) -> list[str]:
    """The guards that are on and ask for `mode = "log"`, by name."""
    return sorted(n for n in pol.guards.on if pol.guards.mode(n) == "log")


RANK = {"pass": 0, "note": 1, "warn": 2, "would-deny": 2, "deny": 3, "error": 4}


def stream(pol, event: str, calls: list, ctx: Context, verdicts: list[dict] | None = None):
    """The findings of the guards that are on at `event`, one at a time, as (guard name, the Call judged or None,
    Finding), in the fixed order (see the module docstring). A caller that stops at the first deny never asks the
    guards after it. On "pre" and "post" each call goes to the guards that read its kind; on the other events the
    guards get one None call. `verdicts` gets one {guard, result (pass, note, warn, would-deny, deny or error), ms} as
    each guard starts, kept up to date as it goes; a guard that raises is `error` and the exception goes on.

    A guard whose mode is log (`pol.guards.mode`) has each deny turned into a note when `ctx.wall_gap()` says the wall is
    proven: the finding is yielded with `would_deny` set (its action is "note", its reason says it was not refused), the
    verdict is `would-deny` and gains a `count` of them, and nothing is asked of a person. When the wall is not proven the
    deny goes on as it is, and the verdict gains `gap`, the word `wall_gap` gave."""
    for g in wanted(pol.guards.on, event):
        cfg = pol.guards.config.get(g.NAME)
        logging = pol.guards.mode(g.NAME) == "log"
        verdict = {"guard": g.NAME, "result": "pass", "ms": 0.0}
        if verdicts is not None:
            verdicts.append(verdict)
        began = time.perf_counter()
        try:
            for call in calls if event in ("pre", "post") else [None]:
                if call is not None and call.kind not in g.KINDS:
                    continue
                found = g.judge(call, cfg, ctx)
                if found is not None and found.action == "deny" and logging:
                    gap = ctx.wall_gap()
                    if gap:
                        verdict["gap"] = gap  # asked to log, and enforcing: the line says why
                    else:
                        found = Finding("note", f"not refused (mode log): {found.reason}", would_deny=True)
                        verdict["count"] = verdict.get("count", 0) + 1
                if found is not None:
                    if found.would_deny:
                        verdict["result"] = "would-deny"
                    elif RANK[found.action] > RANK[verdict["result"]]:
                        verdict["result"] = found.action
                    verdict["ms"] = round((time.perf_counter() - began) * 1000, 2)
                    yield g.NAME, call, found
        except GeneratorExit:  # the caller stopped at this guard's deny: not an error
            raise
        except BaseException:
            verdict["result"] = "error"
            raise
        finally:
            verdict["ms"] = round((time.perf_counter() - began) * 1000, 2)


def run(
    pol, event: str, calls: list, ctx: Context, verdicts: list[dict] | None = None
) -> list[tuple[str, Any, Finding]]:
    """Every finding the guards that are on make at `event`, as (guard name, the Call judged or None, Finding), in the
    fixed order (see the module docstring). No guard is skipped; `stream` is for a caller that stops at a deny."""
    return list(stream(pol, event, calls, ctx, verdicts))
