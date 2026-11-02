"""Open a session or an ask where it lives: which opener offers what, and the one call the page's server may run.

The profile's `[home.open]` picks an opener per kind (`session`, `ask`, `opaque`) by name: `terminal`, `claude_app`,
`browser`, or `command: <program> {tmux}`; several names are separated by commas. With nothing picked a session or an
ask gets every opener that has something to offer, and an opaque record gets the terminal one. An opaque record is only
ever offered an opener that says `OPAQUE_OK`, whatever the profile says, because it carries nothing but its tmux target.
`attach` fills `opens` on the records of a snapshot; `perform` is what a POST runs, after the server has checked the
token, Host and Origin, and it offers the call again from a fresh record first, so a page can't ask for a call that
wasn't on offer. Nothing here uses a shell.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from ws.home import api
from ws.home.openers import Ctx, discover
from ws.home.records import Ask, Session, Snapshot

__all__ = ["attach", "opens_for", "parse", "perform"]

RUN_TIMEOUT_S = 5.0
Runner = Callable[[list[str], float], tuple[int, str]]


def parse(value: str) -> list[tuple[str, str]]:
    """`[home.open]` text as (opener name, argument) pairs: `command: ws-term {tmux}` is one, `a, b` is two."""
    text = value.strip()
    if not text:
        return []
    name, colon, rest = text.partition(":")
    if colon and name.strip() == "command":
        return [("command", rest.strip())]
    return [(n.strip(), "") for n in text.split(",") if n.strip()]


def _slot(rec: Session | Ask) -> str:
    return "opaque" if rec.opaque else ("ask" if isinstance(rec, Ask) else "session")


def _picked(rec: Session | Ask, setting: Mapping[str, str]) -> list[tuple[str, str]] | None:
    """The pairs the profile picked for this record's kind, or None when it picked nothing."""
    value = setting.get(_slot(rec), "")
    pairs = parse(value)
    return pairs or None


def opens_for(rec: Session | Ask, setting: Mapping[str, str], environ: Mapping[str, str]) -> list[dict]:
    """What to offer for `rec`: a dict per way to open it (`opener`, `label`, and `href`, `post` or `text`)."""
    if isinstance(rec, Session) and rec.state == "ended":
        return []
    mods = {m.NAME: m for m in discover()}
    picked = _picked(rec, setting)
    if picked is None:
        picked = [("terminal", "")] if rec.opaque else [(n, "") for n in mods if n != "command"]
    out: list[dict] = []
    seen: set[str] = set()
    for name, arg in picked:
        mod = mods.get(name)
        if mod is None or (rec.opaque and not mod.OPAQUE_OK):
            continue
        ctx = Ctx(environ=environ, arg=arg)
        if not mod.available(ctx)[0]:
            continue
        found = mod.action(rec, ctx)
        if not found:
            continue
        key = found.get("href") or found.get("text") or found["label"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"opener": name, **found})
    return out


def attach(snap: Snapshot, env: api.Env) -> Snapshot:
    """The snapshot with `opens` filled on every session and ask (the one place the openers are asked)."""
    setting, environ = env.cfg.open, env.environ

    def one(rec):
        return replace(rec, opens=tuple(opens_for(rec, setting, environ)))

    sessions = tuple(one(s) for s in snap.sessions)
    lead = next((s for s in sessions if s.lead), None)
    return replace(snap, sessions=sessions, asks=tuple(one(a) for a in snap.asks), lead=lead)


def _find(snap: Snapshot, kind: str, rec_id: str) -> Session | Ask | None:
    pool = snap.asks if kind == "ask" else snap.sessions
    return next((r for r in pool if r.id == rec_id), None)


def perform(env: api.Env, opener: str, kind: str, rec_id: str, run: Runner | None = None) -> tuple[int, dict]:
    """Run the call the page offered for one record: (HTTP status, JSON). 404 for a record that is gone, 409 for a call
    that isn't on offer for it now, 502 when the program fails or isn't there."""
    if kind not in ("session", "ask"):
        return 400, {"error": "kind must be session or ask"}
    snap = api.snapshot(env)
    rec = _find(snap, kind, rec_id)
    if rec is None:
        return 404, {"error": "that session or ask is gone"}
    setting, environ = env.cfg.open, env.environ
    offered = [o for o in opens_for(rec, setting, environ) if o["opener"] == opener and o.get("post")]
    if not offered:
        return 409, {"error": "that isn't offered for it"}
    mods = {m.NAME: m for m in discover()}
    arg = next((a for n, a in (_picked(rec, setting) or []) if n == opener), "")
    line = mods[opener].argv(rec, Ctx(environ=environ, arg=arg))
    if not line:
        return 409, {"error": "that isn't offered for it"}
    try:
        code, _ = (run or env.run)(line, RUN_TIMEOUT_S)
    except (OSError, TimeoutError):
        return 502, {"error": f"{line[0]} didn't run"}
    return (200, {"ok": True}) if code == 0 else (502, {"error": f"{line[0]} stopped with code {code}"})
