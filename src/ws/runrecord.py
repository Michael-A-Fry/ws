"""The run record: one JSON line per headless job in `$WS_HOME/logs/runs.jsonl`, the same shape for every runner.

The shape is `runschema.SCHEMA` (JSON Schema 2020-12, a Python dict so the package stays .py files only). Every key is always
present; a value ws could not learn is null. Appends go through `ws.jsonl`: one `write()` under a lock, so two jobs
finishing together never interleave; the file is 0600, rotated at 5 MB (three old files, more while they hold a record
from this month) and no string field is longer than 16 KiB. `read` and `ws runs` read across the old files.

Schema 2 added `green` (what the job's verification covers) and `recovered` (the automatic recoveries `ws run` tried).
Lines written under schema 1 stay in the log and read as they are; callers use `.get` for the two new keys.
"""

from __future__ import annotations

import copy
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import jsonl
from ws.profile import ws_home
from ws.runschema import SCHEMA

SCHEMA_VERSION = 2
KEYS = (
    "schema",
    "run_id",
    "job",
    "runner",
    "runner_version",
    "exe_path",
    "model_requested",
    "models_used",
    "started_at",
    "ended_at",
    "duration_ms",
    "duration_api_ms",
    "exit_code",
    "status",
    "error",
    "session_id",
    "transcript_path",
    "tokens",
    "cost_usd",
    "cost_basis",
    "num_turns",
    "cwd",
    "policy",
    "secrets_used",
    "ws_version",
    "output_truncated",
    "allow_unpriced",
    "green",
    "recovered",
)
GREEN_LEVELS = ("targeted", "package", "full", "merge_ready")
TOKEN_KEYS = ("input", "output", "cache_creation", "cache_read")
STATUSES = ("ok", "error", "timeout", "refused")


def runs_path(env: dict | None = None) -> Path:
    return ws_home(env) / "logs" / "runs.jsonl"


def schema() -> dict:
    return copy.deepcopy(SCHEMA)


def schema_json() -> str:
    """The schema as JSON text, for a file or a validator that wants one."""
    return json.dumps(SCHEMA, indent=2) + "\n"


def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id(now: datetime | None = None) -> str:
    """Sorts by start time; the random tail keeps two jobs started in the same second apart."""
    now = now or datetime.now(UTC)
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{os.urandom(3).hex()}"


def blank_tokens() -> dict:
    return dict.fromkeys(TOKEN_KEYS)


def new(**fields) -> dict:
    """A record with every key present: null for what is not given, empty lists for the lists."""
    rec: dict = dict.fromkeys(KEYS)
    rec.update(
        schema=SCHEMA_VERSION,
        models_used=[],
        secrets_used=[],
        tokens=blank_tokens(),
        policy={"dial": None, "rule_of_two": None},
        output_truncated=False,
        allow_unpriced=False,
        recovered=[],
    )
    unknown = set(fields) - set(KEYS)
    if unknown:
        raise KeyError(f"not a run record field: {', '.join(sorted(unknown))}")
    rec.update(fields)
    return rec


def _this_month(rec: dict) -> bool:
    """A record from this UTC month or the last: rotation keeps any file that holds one. (Only this month counts against
    the cap; last month's are kept a little longer so a month boundary, or a clock a day out, never loses the spend.)"""
    now = datetime.now(UTC)
    last = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return str(rec.get("started_at", ""))[:7] in (f"{now:%Y-%m}", last)


def append(path: Path, rec: dict, **limits) -> None:
    """Add a record (see `ws.jsonl.append` for the lock, rotation and field cap; `limits` overrides its sizes)."""
    missing = [k for k in KEYS if k not in rec]
    if missing:
        raise ValueError(f"run record is missing {', '.join(missing)}")
    jsonl.append(path, rec, keep_if=_this_month, **limits)


def read(path: Path) -> list[dict]:
    """Every record, oldest first, across the rotated files. A line that is not a JSON object is skipped, so one bad
    write never hides the rest. No log is no records; one that exists but cannot be read raises OSError."""
    return jsonl.read(path)


# What a run counts against the cap when the runner did not say what it cost. ws ships no prices (they go stale), so
# both figures are the person's own, in [roles.budget], and both should be set on the cautious side:
#   fallback_usd_per_mtok  US$ per 1M tokens: a run with token counts counts all its tokens at this rate
#   unknown_run_usd        US$ per run, for a run that started but has no token counts either (default 1.0)
UNKNOWN_RUN_USD = 1.0


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _token_count(rec: dict) -> int | None:
    """All the tokens a record reports (input, output and cache); None when it reports none."""
    tokens = rec.get("tokens") if isinstance(rec.get("tokens"), dict) else {}
    counts = [tokens.get(k) for k in TOKEN_KEYS]
    counts = [v for v in counts if isinstance(v, int) and not isinstance(v, bool) and v >= 0]
    return sum(counts) if counts else None


def _started(rec: dict) -> bool:
    """Did the runner start? (A refusal, a missing runner or an unreachable broker spent nothing.)"""
    if rec.get("status") in ("ok", "timeout"):
        return True
    return rec.get("status") == "error" and (
        rec.get("exit_code") is not None or str(rec.get("error", "")).startswith("interrupted")
    )


def record_cost(rec: dict, unknown_run_usd: float | None = None, fallback_usd_per_mtok: float | None = None) -> float:
    """What a record counts against the cap: its cost when the runner reported one. Otherwise its tokens at
    `fallback_usd_per_mtok` if that is set, else a flat `unknown_run_usd` (default UNKNOWN_RUN_USD) for a run that started.
    Both are conservative figures the person sets; ws does not know any prices."""
    if _finite(rec.get("cost_usd")):
        return float(rec["cost_usd"])
    if not _started(rec):
        return 0.0
    tokens = _token_count(rec)
    if fallback_usd_per_mtok and tokens is not None:
        return tokens / 1_000_000 * float(fallback_usd_per_mtok)
    return float(unknown_run_usd or UNKNOWN_RUN_USD)


def month_spend(
    records: list[dict],
    now: datetime | None = None,
    unknown_run_usd: float | None = None,
    fallback_usd_per_mtok: float | None = None,
) -> float:
    """US$ counted in the UTC month of `now`: reported costs, plus the fallback for runs that reported none."""
    month = f"{(now or datetime.now(UTC)).astimezone(UTC):%Y-%m}"
    return sum(
        record_cost(r, unknown_run_usd, fallback_usd_per_mtok)
        for r in records
        if str(r.get("started_at", "")).startswith(month)
    )
