"""`ws rate`: a thumbs up or down on a run, in one command.

    ws rate <run-id|last> up|down [--note TEXT]

Each rating is one line appended to `$WS_HOME/ratings.jsonl` (rotated at 5 MB into `logs/`): run id, UTC time, `up`
or `down`, an optional note and `who` ("person"). Rating the same run again adds a line; the latest one for a run is
the one that counts. `ws run` prints the matching `ws: rate:` line after its hand-back.

Only the person rates. The file sits in ws's home with the other control files, which a session may not write, so an
agent cannot mark its own work. The id must be one `ws run` recorded in `runs.jsonl`.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl, output, runrecord
from ws.output import WsError
from ws.profile import ws_home

SCHEMA_VERSION = 1
RATINGS = ("up", "down")
NOTE_MAX = 500


def ratings_path(env: dict | None = None) -> Path:
    return ws_home(env) / "ratings.jsonl"


def new(run_id: str, rating: str, note: str | None = None, now: datetime | None = None) -> dict:
    if rating not in RATINGS:
        raise ValueError(f"rating must be one of: {', '.join(RATINGS)}")
    note = " ".join(note.split())[:NOTE_MAX] if note else None
    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "ts": runrecord.iso(now or datetime.now(UTC)),
        "rating": rating,
        "note": note or None,
        "who": "person",
    }


def append(path: Path, rec: dict) -> None:
    """Add a rating through `ws.jsonl` (locked, rotated at 5 MB). The old files and the lock sit under `logs/`, which a
    session may not write: a planted `ratings.jsonl.1` could otherwise rate an agent's own work."""
    jsonl.append(path, rec, side_dir=path.parent / "logs")


def read(path: Path) -> list[dict]:
    """Every rating, oldest first, across the rotated files; a line that is not a JSON object is skipped. No file is
    no ratings."""
    return jsonl.read(path, side_dir=path.parent / "logs")


def latest(ratings: list[dict]) -> dict[str, str]:
    """run id -> its latest rating."""
    return {
        r["run_id"]: r["rating"] for r in ratings if isinstance(r.get("run_id"), str) and r.get("rating") in RATINGS
    }


def resolve(target: str, runs: list[dict]) -> str:
    """The run id for `last` or an id that `runs.jsonl` has. Raises ValueError with a sentence for the person."""
    ids = [r["run_id"] for r in runs if isinstance(r.get("run_id"), str)]
    if target == "last":
        if not ids:
            raise ValueError("no runs yet, so nothing to rate")
        return ids[-1]
    if target not in ids:
        raise ValueError(f"no run {target!r} in the run log; `ws runs` lists them")
    return target


def cmd_rate(args: argparse.Namespace) -> int:
    env = os.environ
    log, path = runrecord.runs_path(env), ratings_path(env)
    try:
        run_id = resolve(args.run, runrecord.read(log))
        earlier = latest(read(path)).get(run_id)
        append(path, new(run_id, args.rating, args.note))
    except ValueError as e:
        raise WsError("not_found", str(e), "`ws runs` lists the runs") from e
    except OSError as e:
        raise WsError("io", f"could not read or write the ratings: {e.strerror or type(e).__name__}") from e
    if args.json:
        output.result({"run_id": run_id, "rating": args.rating, "was": earlier})
        return 0
    tail = f" (was {earlier})" if earlier and earlier != args.rating else ""
    print(f"ws: rated {run_id} {args.rating}{tail}")
    return 0


def register(sub) -> None:
    r = sub.add_parser("rate", help="give a run a thumbs up or down")
    r.add_argument("run", metavar="RUN_ID|last", help="a run id from `ws runs`, or `last`")
    r.add_argument("rating", choices=RATINGS)
    r.add_argument("--note", metavar="TEXT", help="a few words on why")
    r.set_defaults(func=cmd_rate)
