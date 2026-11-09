"""Print how much of its time limit a CI job has used, and a ::warning above 75%.

The job writes the time it started (whole seconds since the epoch) into a stamp file as its first step. This runs as the
last step (`if: always()`), with the job's own `timeout-minutes` as `--timeout-minutes`. It never fails a job: a missing,
empty or unreadable stamp prints one line and exits 0, and a job that passes 75% gets a warning, not a red mark. A job
that hits its limit is stopped by the runner before this step can run, so the warning on the run before is the early
sign; docs/reference/ci.md says so.

    python3 scripts/ci_time_budget.py --job ci --stamp "$RUNNER_TEMP/job-start" --timeout-minutes 15

`--tiers PATH` adds one line for each tier in a `ws.test-tiers/1` report (tests/plugins/ws_harness.py writes it). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

WARN_AT = 0.75  # of the job's timeout-minutes


def read_stamp(path: Path) -> int | None:
    """The start time in seconds, or None when the stamp is missing, empty or not a whole number."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdecimal() else None


def tier_lines(path: Path) -> list[str]:
    """One summary line per tier line of the report (nothing when it isn't there or can't be read)."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for text in raw:
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("schema") == "ws.test-tiers/1":
            out.append(
                f"  tier {row.get('tier')}: {row.get('duration_s')} s of {row.get('budget_s')} s, {row.get('tests')} tests"
            )
    return out


def report(job: str, started: int | None, now: float, timeout_minutes: float) -> tuple[str, bool]:
    """(the text to print, whether it is a warning)."""
    if started is None:
        return f"time budget: {job}: no job-start stamp, nothing to compare (not a failure)", False
    used = max(0.0, now - started) / 60
    share = used / timeout_minutes
    line = f"{job} used {used:.1f} of {timeout_minutes:g} min ({share:.0%})"
    if share > WARN_AT:
        text = (
            f"::warning title=Job {job} is near its time limit::{line}; the warning starts above {WARN_AT:.0%}. "
            "Find what got slower before the runner stops the job."
        )
        return text, True
    return f"time budget: {line}", False


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--job", required=True, help="the job's name, for the message")
    ap.add_argument("--stamp", required=True, type=Path, help="file holding the job's start time in epoch seconds")
    ap.add_argument("--timeout-minutes", required=True, type=float, help="the job's own timeout-minutes")
    ap.add_argument("--tiers", type=Path, help="a ws.test-tiers/1 report to summarise")
    args = ap.parse_args(argv)
    if args.timeout_minutes <= 0:
        print("time budget: --timeout-minutes must be more than zero", file=sys.stderr)
        return 2
    text, _ = report(args.job, read_stamp(args.stamp), time.time() if now is None else now, args.timeout_minutes)
    print(text)
    if args.tiers:
        for line in tier_lines(args.tiers):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
