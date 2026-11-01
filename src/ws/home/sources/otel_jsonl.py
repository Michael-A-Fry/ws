"""Cost per session, from a folder of daily JSON-lines files of API requests (`<UTC date>.jsonl`).

A line with `event` = `api_request` carries `cost_usd` and `sid`, which is the first eight hex digits of the sha256 of
the session id. This source sums the cost of the last 14 days per `sid` and gives it to the session whose id hashes to
it. No other field is read. An opaque session gets no cost here: its cost shows as hidden. The folder is
`[home.sources] otel_dir`.
"""

from __future__ import annotations

import hashlib
import math

from ws.home.records import Session
from ws.home.sources import _files

NAME = "otel_jsonl"
PROVIDES = ("sessions",)
ORDER = 80
RUNNER = "claude-code"
WINDOW_S = 14 * 24 * 3600
EVENTS = ("api_request", "claude_code.api_request")


def available(env):
    return (True, "") if env.path("otel_dir").is_dir() else (False, "no cost folder at the configured path")


def sid_of(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def read(kind, env):
    spent: dict[str, float] = {}
    for path in _files.day_files(env.path("otel_dir"), env.now - WINDOW_S):
        for line in _files.iter_jsonl(path):
            cost, sid = line.get("cost_usd"), line.get("sid")
            counted = (
                isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
            )
            if line.get("event") in EVENTS and isinstance(sid, str) and counted:
                spent[sid] = spent.get(sid, 0.0) + float(cost)
    out = []
    for s in env.sessions_so_far():
        if s.runner == RUNNER and not s.opaque and sid_of(s.id) in spent:
            out.append(Session(id=s.id, source=NAME, runner=RUNNER, cost_usd=spent[sid_of(s.id)], cost_basis="otel"))
    return out
