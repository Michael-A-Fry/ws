"""What the home page needs besides records, and the one call that starts a session.

`build` works the box's pickers out of the snapshot and the profile's `[home]` table: the workspaces (the folders
sessions run in and any the profile names, each with its data class), the machines seen, the installed runners with the
models the profile offers, and which models a workspace's class rules out. `start` is what a POST from the box runs: it
checks every choice against those same lists, then starts the profile's `[home.start] command` with no shell. Nothing
here writes a file; the program the profile names does the starting.
"""

from __future__ import annotations

from ws import adapters
from ws.home import api, template
from ws.home.areas._base import Hub
from ws.home.records import Snapshot

__all__ = ["WHAT", "build", "start"]

WHAT = ("session", "chat", "job")
OPAQUE_CLASS = "terminal only"  # a workspace only opaque sessions were seen in
REQUEST_MAX = 4000
START_TIMEOUT_S = 30.0


def _denied(cfg: api.HomeConfig, runner: str, model: str) -> tuple[str, ...]:
    """The data classes a runner, or one model of it, can't take, from `[home.deny]`."""
    keys = [runner, f"{runner}/{model}"] if model else [runner]
    return tuple(dict.fromkeys(c for k in keys for c in cfg.deny.get(k, ())))


def _runners(cfg: api.HomeConfig) -> tuple:
    names = list(adapters.all_adapters())
    names += [n for n in cfg.models if n not in names]
    out = []
    for name in names:
        models = cfg.models.get(name) or ("",)  # "" is the runner's own default model
        out.append({"name": name, "models": tuple({"name": m, "deny": _denied(cfg, name, m)} for m in models)})
    return tuple(out)


def _workspaces(snap: Snapshot, cfg: api.HomeConfig) -> tuple:
    seen: dict[str, bool] = {}  # name -> every session seen there was opaque
    live: dict[str, int] = {}
    for s in snap.sessions:
        if s.repo and s.state != "ended":  # a folder only finished jobs ran in isn't a place to start work
            seen[s.repo] = seen.get(s.repo, True) and s.opaque
            live[s.repo] = live.get(s.repo, 0) + 1
    names = sorted({*cfg.workspaces, *seen}, key=lambda n: (-live.get(n, 0), n.casefold()))
    return tuple({"name": n, "cls": cfg.workspaces.get(n) or (OPAQUE_CLASS if seen.get(n) else "")} for n in names)


def build(snap: Snapshot, env: api.Env) -> Hub:
    """The hub for one snapshot. Nothing here reads a file."""
    cfg = env.cfg
    workspaces = _workspaces(snap, cfg)
    here = snap.host or "this machine"
    hosts = [here, *sorted({s.host for s in snap.sessions if s.host and s.host != here}, key=str.casefold)]
    ids = frozenset(a.id for a in snap.asks if api.answerable(a)) if cfg.answer else frozenset()
    return Hub(
        start=bool(cfg.start),
        answer_set=bool(cfg.answer),
        answerable=ids,
        workspaces=workspaces,
        machines=tuple(hosts),
        runners=_runners(cfg),
        classes={w["name"]: w["cls"] for w in workspaces},
    )


def start(env: api.Env, data: dict) -> tuple[int, dict]:
    """Start a session from the box: (HTTP status, JSON). `data` holds `what`, `workspace`, `where`, `runner`, `model` and
    `request`. 409 when `[home.start] command` isn't set or a choice isn't on offer (a model the workspace's class rules
    out included), 502 when the program isn't there or stops with an error."""
    cfg = env.cfg
    if not cfg.start:
        return 409, {"error": "Set [home.start] command to start sessions here"}
    request = data.get("request")
    if not isinstance(request, str) or not request.strip() or len(request) > REQUEST_MAX:
        return 400, {"error": f"write what you want done, in at most {REQUEST_MAX} characters"}
    snap = api.snapshot(env)
    hub = build(snap, env)
    what, workspace, where, runner, model = (
        str(data.get(k, "")) for k in ("what", "workspace", "where", "runner", "model")
    )
    if what not in WHAT:
        return 409, {"error": "that isn't something ws can start"}
    if workspace not in hub.classes:
        return 409, {"error": "that workspace isn't one ws knows"}
    if where not in hub.machines:
        return 409, {"error": "that machine isn't one ws has seen"}
    pick = next((r for r in hub.runners if r["name"] == runner), None)
    chosen = next((m for m in pick["models"] if m["name"] == model), None) if pick else None
    if chosen is None:
        return 409, {"error": "that runner and model aren't on offer"}
    cls = hub.classes[workspace]
    if cls and cls in chosen["deny"]:
        return 409, {"error": f"{cls} data can't go to {runner}{'/' + model if model else ''} (your [home.deny])"}
    values = {
        "what": what,
        "workspace": workspace,
        "where": where,
        "runner": runner,
        "model": model,
        "request": request,
    }
    try:
        argv = template.fill(cfg.start, values)
    except ValueError as e:
        return 400, {"error": str(e)}
    try:
        code, _ = env.run(argv, START_TIMEOUT_S)
    except (OSError, TimeoutError):
        return 502, {"error": f"{argv[0]} didn't run"}
    return (200, {"ok": True}) if code == 0 else (502, {"error": f"{argv[0]} stopped with code {code}"})
