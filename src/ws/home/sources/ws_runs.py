"""Headless jobs as sessions: the run log (`$WS_HOME/logs/runs.jsonl`), one session per finished job with its cost."""

from __future__ import annotations

import math

from ws import runrecord
from ws.home.records import NOT_MEASURED, Session
from ws.home.sources import UNREADABLE, SourceError, _files

NAME = "ws_runs"
PROVIDES = ("sessions",)
ORDER = 30
WINDOW_S = 14 * 24 * 3600  # how far back the runs reach
KEEP = 100  # the newest runs that are kept


def available(env):
    path = runrecord.runs_path(dict(env.environ))
    return (True, "") if path.exists() or runrecord.fallback_path(path).exists() else (False, "no run log yet")


def _tokens(rec) -> dict | None:
    raw = rec.get("tokens")
    if not isinstance(raw, dict):
        return None
    got = {k: raw[k] for k in runrecord.TOKEN_KEYS if isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool)}
    return {k: got.get(k, 0) for k in runrecord.TOKEN_KEYS} if got else None


def read(kind, env):
    try:
        recs = runrecord.read(runrecord.runs_path(dict(env.environ)))
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't read the run log: {e.strerror or type(e).__name__}") from e
    cutoff = env.now - WINDOW_S
    out = []
    for rec in recs:
        started = _files.iso(rec.get("started_at"))
        if rec.get("eval") or not started or _files.epoch(started) < cutoff:  # trials show under evals
            continue
        sid = rec.get("session_id") if isinstance(rec.get("session_id"), str) and rec["session_id"] else None
        ident = sid or rec.get("run_id")
        if not isinstance(ident, str) or not ident:
            continue
        cost = rec.get("cost_usd")
        has_cost = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
        cwd = _files.text(rec.get("cwd"), 500)
        basis = rec.get("cost_basis") if isinstance(rec.get("cost_basis"), str) and rec["cost_basis"] else "reported"
        out.append(
            Session(
                id=ident,
                source=NAME,
                runner=_files.text(rec.get("runner"), 40),
                name=_files.text(rec.get("job"), 80),
                cwd=cwd,
                repo=_files.repo_label(cwd),
                state="ended",
                started_at=started,
                last_seen=_files.iso(rec.get("ended_at")) or started,
                tokens=_tokens(rec),
                cost_usd=float(cost) if has_cost else None,
                cost_basis=_files.text(basis, 40) if has_cost else NOT_MEASURED,
            )
        )
    return out[-KEEP:]
