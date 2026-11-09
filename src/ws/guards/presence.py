"""presence: record in the session registry whether a session is busy or idle, from the hooks it already runs.

A prompt, the start of a tool call and the end of one say `busy`; the start of a session and a stop say `idle`
(`HOOK_STATE`). It only records, so it runs first (priority 0, a call another guard refuses is on record too), it never
returns a finding, and any failure is swallowed: a hook that reports presence must not slow, block or fill the screen of
the session it reports on, and before a call a raised error would be a deny. A hook fired by a sub-agent (its payload has
an `agent_id`) is not the session's own state and is skipped.

Order. Hooks of one session run as separate processes, so a late `pre` can arrive after the `stop` that followed it. Each
report carries `seq`, the hook's own clock in nanoseconds, and a `source` (`hook`; a source of lower authority never
overrides a fresher one of higher authority, which is where a later source such as the screen would sit). The newest
`seq` seen is kept in the guard's own record for the session (`<state>/sessions/<id>.presence.json`) under its lock, and
the registry is written under that same lock, so an older report is dropped and cannot overwrite a newer one. The write
is `session.update` with `expect_rev`: if the record changed between the read and the write it is read again and decided
again, once. A state that has not changed in the last `REFRESH_S` seconds is not written again, so the registry does not
churn on every tool call.

Fresh or not. `STALE_S` (two minutes) is the one window every reader uses: `effective_state(record, now)` gives `busy`
only while the last busy report is that young. A `busy` that has gone quiet for longer (a long tool call, or a session
that died with no stop) reads as None, which every reader treats as "can't tell", the strictest case; an `idle` stays
`idle`, because a session that has stopped sends no more hooks until a person speaks to it. On a stop or a prompt the
guard also records `last_delivered_outbound`, the latest message the session sent that its receiver has had delivered.
No screen is read: that is a later source for the same fields.
"""

from __future__ import annotations

from datetime import datetime

from ws import session
from ws.guards import Finding, known_keys
from ws.output import WsError

NAME = "presence"
EVENTS = ("pre", "post", "start", "prompt", "stop")
KINDS = ("command", "read", "search", "write", "tool", "agent", "skill", "message")
PRIORITY = 0

STALE_S = 120  # a busy report older than this is no longer evidence of anything (read by pane-close and inbox too)
REFRESH_S = 30  # an unchanged state is written again only after this long
HOOK_STATE = {"start": "idle", "prompt": "busy", "pre": "busy", "post": "busy", "stop": "idle"}
AUTHORITY = {"hook": 2}  # source -> rank; a lower rank can't override a fresh report of a higher one
STATE = "presence"


def config(table: dict, where: str) -> dict:
    return known_keys(table, where)


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else None
    except ValueError:
        return None


def effective_state(rec: session.Record | None, now: float) -> str | None:
    """`busy`, `idle`, or None (can't tell) for a record at time `now`. A busy report older than `STALE_S`, one with no
    readable time, a record that is missing and a state outside busy/idle are None."""
    if rec is None or rec.state not in session.STATES:
        return None
    if rec.state == "idle":
        return "idle"
    at = _epoch(rec.state_ts)
    return "busy" if at is not None and 0 <= now - at <= STALE_S else None


def _outbound(session_id: str) -> dict | None:
    from ws import msg

    try:
        return msg.last_delivered_outbound(session_id)
    except WsError:
        return None


def _write(ctx, state: str) -> None:
    """Read the record, decide, write with the rev that was read; once more if it changed in between."""
    for _ in range(2):
        rec = session.read(ctx.session_id)
        if rec is None:
            return
        changes: dict = {}
        fresh = _epoch(rec.state_ts)
        if rec.state != state or fresh is None or ctx.now() - fresh >= REFRESH_S:
            changes["state"] = state
        if ctx.event in ("stop", "prompt"):
            sent = _outbound(ctx.session_id)
            if sent is not None and sent != rec.last_delivered_outbound:
                changes["last_delivered_outbound"] = sent
        if not changes:
            return
        try:
            session.update(ctx.session_id, expect_rev=rec.rev, **changes)
            return
        except WsError as e:
            if e.kind != "conflict":
                return


def continued(ctx) -> None:
    """A Stop that a hook is continuing: the session is working, though this guard has just written idle. Only over a
    state this guard wrote (with presence off the record has none, and nothing would ever clear a busy)."""
    try:
        record = session.read(ctx.session_id)
        if record is not None and record.state is not None:
            _report(ctx, "busy")
    except Exception:  # noqa: BLE001 - it only records; nothing it meets is a reason to fail a hook
        return


def _report(ctx, state: str | None = None) -> None:
    state = state or HOOK_STATE.get(ctx.event)
    if state is None or not ctx.session_id or ctx.payload.get("agent_id"):
        return
    seq = int(ctx.now() * 1_000_000_000)

    def note(held: dict) -> None:
        if seq < held.get("seq", 0):
            return  # an older report than the one we hold: drop it
        mine, theirs = AUTHORITY["hook"], AUTHORITY.get(held.get("source"), 0)
        newest = held.get("seq", 0) / 1_000_000_000
        if theirs > mine and ctx.now() - newest <= STALE_S:
            return
        _write(ctx, state)
        held.update(seq=seq, source="hook", state=state)

    ctx.state(STATE, note)


def judge(call, cfg, ctx) -> Finding | None:
    try:
        _report(ctx)
    except Exception:  # noqa: BLE001 - it only records; nothing it meets is a reason to fail a hook
        return None
    return None
