"""The run record: one JSON line per headless job in `$WS_HOME/logs/runs.jsonl`, the same shape for every runner.

The shape is `runschema.SCHEMA` (JSON Schema 2020-12, a Python dict so the package stays .py files only). Every key is always
present; a value ws could not learn is null. Appends are one `write()` on an O_APPEND fd under an flock, so two jobs
finishing together never interleave, and the file is 0600.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from ws.profile import ws_home
from ws.runschema import SCHEMA

try:  # not on native Windows; ws runs there only through WSL2, but the module must still import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
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
)
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
    )
    unknown = set(fields) - set(KEYS)
    if unknown:
        raise KeyError(f"not a run record field: {', '.join(sorted(unknown))}")
    rec.update(fields)
    return rec


def append(path: Path, rec: dict) -> None:
    missing = [k for k in KEYS if k not in rec]
    if missing:
        raise ValueError(f"run record is missing {', '.join(missing)}")
    line = (json.dumps(rec, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        st = os.fstat(fd)
        if st.st_mode & 0o077 and st.st_uid == os.geteuid():  # made earlier with a wider mode
            os.fchmod(fd, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        os.close(fd)  # closing also drops the lock


def read(path: Path) -> list[dict]:
    """Every record, oldest first. A line that is not a JSON object is skipped, so one bad write never hides the rest.
    A missing file is no records; one that exists but cannot be read raises OSError."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
    return out


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
