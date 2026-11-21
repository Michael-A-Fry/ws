"""The step-in counter: `ws passtest events` reads a pass-test ledger and prints confirmed step-ins per week, by kind and by area, with the trend.

A step-in is a time a person had to come back and steer an AI tool: dig into its work, reframe the task, correct it,
follow up on something it left, or do by hand what was meant to run by itself. The counter answers one question:
is that happening less often, week by week? It only reads; it never writes to the ledger, and nothing starts the
count over.

The ledger is an append-only jsonl file written by a separate helper, one JSON object per line:
`{"ts": "<ISO 8601 UTC>", "kind": "<kind>", "data": {...}, "prev": "<sha256 of the previous line>"}`. The first line's
`prev` is 64 zeros. The counter checks that chain before it counts anything, and refuses a broken one (exit 1, with
the line number), so a rewritten ledger cannot give a clean count. The line kinds it reads, and their `data`:

- `proposal`: `{id, event_kind, area, about}`; `event_kind` is one of `dig`, `reframe`, `correct`, `follow-up`,
  `not-automatic`.
- `confirm` and `dismiss`: `{proposal_id}`. The latest of these for a proposal decides it.
- `job-run`: `{job, run_id, status, expected_at}`; `status` is `ok`, `failed` or `missed`. A `failed` or `missed` run
  is one `not-automatic` step-in, counted once per `run_id` however often it is logged. The counter never invents an
  expected run: a missed run is one the job runtime wrote down as missed.

A proposal counts as confirmed when a confirm names it, or when it has stayed open `OPEN_DAYS` (7) days with neither
a confirm nor a dismiss. An event falls in the week (Monday 00:00 UTC) of its proposal's `ts`, or of a run's
`expected_at` (its `ts` when that is absent). `now` is a parameter of `count`, so the 7-day rule is testable; the
command uses the clock.

Trend: the least-squares slope of the weekly totals over the weeks shown; rising above +0.5 a week, falling below -0.5,
flat in between.

Settings live outside ws: a TOML file named by `--config` or `$WS_PASSTEST_CONFIG`, with `ledger` (a path, relative
to the file) and `areas` (the areas always shown, in order). `$WS_PASSTEST_LEDGER` overrides `ledger`. There is no
built-in ledger path.

Swap note: the ledger is plain jsonl and this module is its only reader. To count from another source, replace
`read_ledger` with a function that returns the same `Line` records (verified, in order); `count` and the output stay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import output
from ws.output import WsError

COMMAND = "passtest"

EVENT_KINDS = ("dig", "reframe", "correct", "follow-up", "not-automatic")
RUN_STATUSES = ("ok", "failed", "missed")
OPEN_DAYS = 7
TREND_BAND = 0.5
GENESIS = "0" * 64
NO_AREA = "(none)"
CONFIG_ENV = "WS_PASSTEST_CONFIG"
LEDGER_ENV = "WS_PASSTEST_LEDGER"
CONFIG_KEYS = frozenset({"ledger", "areas"})
MAX_WEEKS = 520


@dataclass(frozen=True)
class Line:
    """One verified ledger line."""

    n: int
    ts: datetime
    kind: str
    data: dict


@dataclass(frozen=True)
class Event:
    """One confirmed step-in."""

    at: datetime
    kind: str
    area: str
    source: str  # "proposal:<id>" or "job-run:<run_id>"


@dataclass
class Counts:
    weeks: list[datetime]
    by_kind: list[dict[str, int]]
    by_area: list[dict[str, int]]
    totals: list[int]
    slope: float
    trend: str
    open: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    ledger: Path | None
    areas: tuple[str, ...]
    source: str  # where the settings came from, for messages


def _when(value: object) -> datetime | None:
    """An ISO 8601 time as an aware UTC datetime; a time with no zone is read as UTC. None if it isn't one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


def load_config(path: str | None = None, environ: dict[str, str] | None = None) -> Config:
    """The settings: `path`, else `$WS_PASSTEST_CONFIG`, else none. The whole file validates or this raises."""
    env = os.environ if environ is None else environ
    name = path or env.get(CONFIG_ENV) or ""
    ledger: Path | None = None
    areas: tuple[str, ...] = ()
    source = "no config"
    if name:
        file = Path(name).expanduser()
        source = str(file)
        try:
            raw = tomllib.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WsError(
                "bad_config", f"the pass-test config {file} doesn't exist", f"create it, or unset ${CONFIG_ENV}", 2
            ) from None
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise WsError(
                "bad_config", f"the pass-test config {file} can't be read: {exc}", "fix the file", 2
            ) from None
        extra = sorted(set(raw) - CONFIG_KEYS)
        if extra:
            raise WsError(
                "bad_config", f"{file}: unknown key(s) {', '.join(extra)}", "the keys are `ledger` and `areas`", 2
            )
        if "ledger" in raw:
            if not isinstance(raw["ledger"], str) or not raw["ledger"]:
                raise WsError(
                    "bad_config", f"{file}: `ledger` must be a path", 'e.g. ledger = "/var/lib/ledger.jsonl"', 2
                )
            ledger = Path(raw["ledger"]).expanduser()
            if not ledger.is_absolute():
                ledger = file.parent / ledger
        got = raw.get("areas", [])
        if not isinstance(got, list) or not all(isinstance(a, str) and a for a in got):
            raise WsError("bad_config", f"{file}: `areas` must be a list of names", 'e.g. areas = ["build", "docs"]', 2)
        areas = tuple(dict.fromkeys(got))
    if env.get(LEDGER_ENV):
        ledger = Path(env[LEDGER_ENV]).expanduser()
    return Config(ledger, areas, source)


def read_ledger(path: Path) -> list[Line]:
    """Every line of the ledger, after checking the hash chain. Raises WsError on a missing file or a broken chain."""
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        raise WsError(
            "not_found",
            f"the ledger {path} doesn't exist",
            f"install the ledger, or point ${LEDGER_ENV} or the config's `ledger` at it",
        ) from None
    except OSError as exc:
        raise WsError(
            "not_found", f"the ledger {path} can't be read: {exc.strerror}", "check its permissions"
        ) from None
    lines: list[Line] = []
    prev, n = GENESIS, 0
    for n, raw in enumerate((ln for ln in blob.split(b"\n") if ln), 1):
        try:
            entry = json.loads(raw)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            raise _broken(path, n, "not a JSON object")
        if entry.get("prev") != prev:
            raise _broken(path, n, "chain broken (a line before it was changed, removed or reordered)")
        prev = hashlib.sha256(raw).hexdigest()
        ts = _when(entry.get("ts"))
        data = entry.get("data")
        kind = entry.get("kind") if ts is not None and isinstance(entry.get("kind"), str) else ""  # "" is skipped
        lines.append(Line(n, ts or datetime.min.replace(tzinfo=UTC), kind, data if isinstance(data, dict) else {}))
    if blob and not blob.endswith(b"\n"):
        raise _broken(path, n, "truncated (no final newline)")
    return lines


def _broken(path: Path, n: int, why: str) -> WsError:
    return WsError(
        "broken_ledger",
        f"the ledger {path} fails its check at line {n}: {why}",
        "nothing was counted; restore the ledger from a copy and append a correcting line, never edit one",
        1,
        {"line": n},
    )


def events(lines: Iterable[Line], now: datetime) -> tuple[list[Event], int, list[str]]:
    """The confirmed step-ins, the number of proposals still open, and why any line was skipped."""
    proposals: dict[str, Line] = {}
    decided: dict[str, str] = {}
    runs: dict[str, Event] = {}
    skipped: list[str] = []
    for ln in lines:
        d = ln.data
        if ln.kind == "proposal":
            pid, kind = d.get("id"), d.get("event_kind")
            if not isinstance(pid, str) or not pid:
                skipped.append(f"line {ln.n}: proposal with no id")
            elif kind not in EVENT_KINDS:
                skipped.append(f"line {ln.n}: proposal {pid} has an unknown event_kind {kind!r}")
            elif pid in proposals:
                skipped.append(f"line {ln.n}: proposal {pid} again (the first one counts)")
            else:
                proposals[pid] = ln
        elif ln.kind in ("confirm", "dismiss"):
            pid = d.get("proposal_id")
            if isinstance(pid, str) and pid:
                decided[pid] = ln.kind  # the latest decision wins
            else:
                skipped.append(f"line {ln.n}: {ln.kind} with no proposal_id")
        elif ln.kind == "job-run":
            status, rid = d.get("status"), d.get("run_id")
            if status not in RUN_STATUSES:
                skipped.append(f"line {ln.n}: job-run with an unknown status {status!r}")
            elif status != "ok":
                if not isinstance(rid, str) or not rid:
                    skipped.append(f"line {ln.n}: {status} job-run with no run_id")
                elif rid not in runs:
                    at = _when(d.get("expected_at")) or ln.ts
                    area = d.get("area") if isinstance(d.get("area"), str) and d.get("area") else NO_AREA
                    runs[rid] = Event(at, "not-automatic", area, f"job-run:{rid}")
        elif ln.kind == "":
            skipped.append(f"line {ln.n}: no usable kind or ts")
        # any other kind (version-closed, aiw-off, ...) is not a step-in
    found: list[Event] = []
    still_open = 0
    for pid, ln in proposals.items():
        verdict = decided.get(pid)
        if verdict == "dismiss":
            continue
        if verdict is None and now - ln.ts < timedelta(days=OPEN_DAYS):
            still_open += 1
            continue
        area = ln.data.get("area") if isinstance(ln.data.get("area"), str) and ln.data.get("area") else NO_AREA
        found.append(Event(ln.ts, ln.data["event_kind"], area, f"proposal:{pid}"))
    found.extend(runs.values())
    return sorted(found, key=lambda e: (e.at, e.source)), still_open, skipped


def week_start(t: datetime) -> datetime:
    """Monday 00:00 UTC of the week holding `t`."""
    t = t.astimezone(UTC)
    return datetime(t.year, t.month, t.day, tzinfo=UTC) - timedelta(days=t.weekday())


def slope(values: Sequence[float]) -> float:
    """The least-squares slope of `values` against 0, 1, 2, ...; 0 for fewer than two values."""
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    return sum((x - mx) * (y - my) for x, y in enumerate(values)) / sum((x - mx) ** 2 for x in range(n))


def trend_of(s: float) -> str:
    return "rising" if s > TREND_BAND else "falling" if s < -TREND_BAND else "flat"


def count(lines: Iterable[Line], weeks: int, now: datetime, areas: Sequence[str] = ()) -> Counts:
    """Confirmed step-ins for the `weeks` weeks up to and including the week holding `now`."""
    if weeks < 1:
        raise WsError("usage", "--weeks must be 1 or more", "e.g. --weeks 4", 2)
    found, still_open, skipped = events(lines, now)
    last = week_start(now)
    starts = [last - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    index = {s: i for i, s in enumerate(starts)}
    seen = list(areas) + sorted({e.area for e in found if e.area not in areas and week_start(e.at) in index})
    by_kind = [dict.fromkeys(EVENT_KINDS, 0) for _ in starts]
    by_area = [dict.fromkeys(seen, 0) for _ in starts]
    for e in found:
        i = index.get(week_start(e.at))
        if i is not None:
            by_kind[i][e.kind] += 1
            by_area[i][e.area] += 1
    totals = [sum(k.values()) for k in by_kind]
    s = slope(totals)
    return Counts(starts, by_kind, by_area, totals, s, trend_of(s), still_open, skipped)


def _table(head: str, cols: Sequence[str], rows: list[tuple[str, list[int]]]) -> list[str]:
    widths = [max(len(c), 3) for c in cols]
    out = [f"{head:<10}  " + "  ".join(f"{c:>{w}}" for c, w in zip(cols, widths, strict=True))]
    for label, vals in rows:
        out.append(f"{label:<10}  " + "  ".join(f"{v:>{w}}" for v, w in zip(vals, widths, strict=True)))
    return out


def render(c: Counts, ledger: Path, n_lines: int) -> str:
    days = [w.date().isoformat() for w in c.weeks]
    out = [
        f"ws: step-ins confirmed per week, {len(days)} week(s) from {days[0]} ({ledger}: {n_lines} line(s), chain intact)",
        "",
    ]
    kinds = list(EVENT_KINDS) + ["total"]
    out += _table("week", kinds, [(d, [*k.values(), t]) for d, k, t in zip(days, c.by_kind, c.totals, strict=True)])
    areas = list(c.by_area[0]) if c.by_area else []
    if areas:
        out += ["", *_table("by area", areas, [(d, list(a.values())) for d, a in zip(days, c.by_area, strict=True)])]
    out += ["", f"trend: {c.trend} ({c.slope:+.2f} a week; flat inside ±{TREND_BAND})"]
    if c.open:
        out.append(f"open: {c.open} proposal(s) not yet confirmed or dismissed (each counts once {OPEN_DAYS} days old)")
    for why in c.skipped:
        out.append(f"skipped {why}")
    return "\n".join(out)


def _now() -> datetime:
    return datetime.now(UTC)


def cmd_events(args: argparse.Namespace) -> int:
    if not 1 <= args.weeks <= MAX_WEEKS:
        raise WsError("usage", f"--weeks must be from 1 to {MAX_WEEKS}", "e.g. --weeks 4", 2)
    cfg = load_config(args.config)
    if cfg.ledger is None:
        raise WsError(
            "no_ledger",
            f"no ledger to read ({cfg.source})",
            f"set `ledger` in the file ${CONFIG_ENV} names, or set ${LEDGER_ENV}",
        )
    lines = read_ledger(cfg.ledger)
    c = count(lines, args.weeks, _now(), cfg.areas)
    if args.json:
        output.result(
            {
                "ledger": str(cfg.ledger),
                "lines": len(lines),
                "weeks": [
                    {"week": w.date().isoformat(), "by_kind": k, "by_area": a, "total": t}
                    for w, k, a, t in zip(c.weeks, c.by_kind, c.by_area, c.totals, strict=True)
                ],
                "trend": {"direction": c.trend, "slope": round(c.slope, 4), "band": TREND_BAND},
                "open": c.open,
                "skipped": c.skipped,
            }
        )
    else:
        print(render(c, cfg.ledger, len(lines)))
    return 0


def register(sub) -> None:
    p = sub.add_parser("passtest", help="the step-in counter: how often a person had to steer, week by week")
    ps = p.add_subparsers(dest="passtest_command", required=True)
    e = ps.add_parser("events", help="confirmed step-ins per week, by kind and by area, with the trend")
    e.add_argument("--weeks", type=int, default=4, help="how many weeks, ending with this one (default 4)")
    e.add_argument("--config", help=f"the settings file (default: ${CONFIG_ENV})")
    e.set_defaults(func=cmd_events)
