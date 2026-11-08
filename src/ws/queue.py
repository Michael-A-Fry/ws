"""Admission before work: nothing starts until its money, turns, time, a slot and memory are reserved, and every run settles from what it used.

    ws queue add JOB [--key K] [--deadline T]   put a job file's run in the queue (unattended work)
    ws queue list [--all] [--last N]            the receipts: queued, running and finished, newest first
    ws queue show RUN_ID                        one receipt
    ws queue cancel RUN_ID                      take a queued run out of the queue
    ws queue run [--max N]                      run what is queued, one after another (what a timer runs)
    ws queue slot --ram-gb N --lane ui --who join|writer -- CMD...
                                                wait for a slot, run CMD in it, give the slot back

One SQLite database in WAL mode, `<state>/queue/queue.db` (folder 0700, file 0600), holds the queued work (the
`ws._vendor.sqlqueue` engine, table `work`) and the receipts of every run, queued or not. `ws run` reserves through the
same code, in one `BEGIN IMMEDIATE` transaction, so a direct run and a queued one count against the same counters:

  * money: the profile's monthly cap and, for a job with a `purpose`, that purpose's cap in `[budget.purposes]`. Spend is
    the month's records in `runs.jsonl` plus the open reservations (a settled reservation is not counted again: its
    record is). Every window is the UTC calendar month (`policy.budget_month`), read from one clock reading.
  * turns and seconds in flight, concurrency (per lane: `ui` holds one at a time), and host memory: MemAvailable must stay at
    or above `reserve_pct` of the total after the job's `ram_gb`. A system that cannot report memory admits, and the
    reservation says so.
  * the subscription limits Claude Code reports, for a job that uses a login rather than a key: a fresh capture at 100
    percent refuses it; no fresh capture is unknown, and unknown is never room.

Money that is short is refused, naming the counter, exit 3. A counter with room under the estimate resizes the
reservation to what is left, and that sum becomes `--max-budget-usd` (Claude Code), unless the job says `strict`.
Turns, time, concurrency and memory are not refusals but waits: a direct `ws run` ends as refused, a queued run stays
queued, and `ws queue slot` waits. A join's `ui` run is admitted before any writer's, and a writer's only when its memory
fits beside the headroom kept for the join, so a writer waits and is never refused for it. If the store is locked, full
or unreadable, the run is refused: it never starts without a reservation.

A run settles once, from what it used, in the path that writes its record (status, cost, tokens, stop reason). Settling
twice changes nothing, and only the claim that reserved can settle: no command settles or frees a reservation for
someone else. A crash leaves a row whose owner (process id and start time) is gone; the next admission reaps it, writes
the run's record at the fallback cost and releases the reservation. Retries: exit 3 never; 143 is `preempted`, settled
at its actuals and requeued (a `preemptible` job, if the deadline allows); a timeout, an unreachable broker or an
interruption backs off 30 s, 1 m, 5 m, three tries; anything else is permanent. A deadline that has passed ends a run as
`expired`, without running it. A queued job must use an API-key credential through `[[secrets]]` or a local provider:
unattended work never draws on a subscription login.

`add`, `cancel` and `run` are refused in a gated session; `list`, `show` and `slot` are not. Limits default to the
constants below and are read from `[roles.queue]` in the profile.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import os
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import approvals, jobs, output, paths, policy, runrecord, usage
from ws._vendor import sqlqueue
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path

COMMAND = "queue"
SCHEMA_VERSION = 1
EXIT_FAILED, EXIT_BAD_JOB, EXIT_REFUSED = 1, 2, 3
STATUSES = ("queued", "running", "ok", "error", "refused", "preempted", "skipped", "interrupted", "expired")
ACTIVE = ("queued", "running")
WORK_TABLE = "work"

# Limits. Each is a default; `[roles.queue]` in the profile overrides it.
MAX_CONCURRENT = 4  # runs at once
UI_SLOTS = 1  # runs at once in the `ui` lane
MAX_OPEN_TURNS = 2000  # turns reserved by runs in flight
MAX_OPEN_SECONDS = 86400.0  # seconds reserved by runs in flight
DEFAULT_TURNS = 30  # reserved for a job that names no max_turns
RESERVE_PCT = 15.0  # MemAvailable must stay at or above this share of the total
JOIN_HEADROOM_GB = 2.0  # memory kept free beside a writer's ui run, for a join's
RAMP_S = 60.0  # a new run's declared memory counts on top of MemAvailable this long, while it grows into it
LEASE_SLACK_S = 120.0  # a reservation's lease is the job's timeout plus this
SLOT_LEASE_S = 6 * 3600.0  # the lease of a slot, which has no timeout of its own
BUSY_S = 5.0  # how long to wait for the store's lock before refusing
POLL_S = 5.0  # between looks, while waiting for a slot
ORPHAN_S = 120.0  # a claimed message with no running receipt this long is released
MESSAGE_LEASE_S = 24 * 3600.0
MIN_RESERVE_USD = 0.0001

# Retries
BACKOFF_S = (30, 60, 300, 900, 3600)
MAX_RETRIES = 3
WAIT_RETRY_S = 30
PREEMPT_REQUEUE_S = 30
TRANSIENT_STOPS = frozenset({"timeout", "broker_unavailable", "interrupted"})

_LIMIT_KEYS = {
    "max_concurrent": ("int", MAX_CONCURRENT),
    "ui_slots": ("int", UI_SLOTS),
    "max_open_turns": ("int", MAX_OPEN_TURNS),
    "max_open_seconds": ("num", MAX_OPEN_SECONDS),
    "default_turns": ("int", DEFAULT_TURNS),
    "reserve_pct": ("pct", RESERVE_PCT),
    "join_headroom_gb": ("num0", JOIN_HEADROOM_GB),
    "ramp_s": ("num0", RAMP_S),
    "busy_s": ("num0", BUSY_S),
    "poll_s": ("num", POLL_S),
}

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS receipts (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    purpose TEXT,
    lane TEXT NOT NULL DEFAULT 'run',
    who TEXT,
    status TEXT NOT NULL CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    month TEXT NOT NULL,
    queued_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    reserved_usd REAL NOT NULL DEFAULT 0,
    reserved_turns INTEGER NOT NULL DEFAULT 0,
    reserved_s REAL NOT NULL DEFAULT 0,
    ram_gb REAL NOT NULL DEFAULT 0,
    resized INTEGER NOT NULL DEFAULT 0,
    lease_expires_at INTEGER,
    owner_pid INTEGER,
    owner_start_time TEXT,
    claim_id INTEGER NOT NULL DEFAULT 0,
    message_id TEXT,
    deadline INTEGER,
    settled INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
    actual_usd REAL,
    tokens TEXT,
    record_written INTEGER NOT NULL DEFAULT 1,
    stop_reason TEXT,
    note TEXT,
    CHECK ((status IN ('queued', 'running')) = (finished_at IS NULL)),
    CHECK (settled = 0 OR finished_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS receipts_one_running ON receipts(job) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS receipts_month ON receipts(month, status);
CREATE INDEX IF NOT EXISTS receipts_message ON receipts(message_id);
"""


# ---------------------------------------------------------------------------------------------------------------------
# Typed errors, limits and the shapes admission passes around


class StoreError(WsError):
    """The queue database could not be opened or written. `reason` is `locked`, `full`, `readonly` or `unreadable`,
    from SQLite's own error code, never from its message."""

    def __init__(self, reason: str, detail: str = "") -> None:
        hints = {
            "locked": "another ws process holds the queue database; try again in a moment",
            "full": "free some disk space; the queue database cannot be written",
            "readonly": "the queue folder is read-only here; run this from a shell that can write ws's state folder",
            "unreadable": "the queue database cannot be read; look at the queue folder in ws's state folder",
        }
        super().__init__(
            f"store_{reason}",
            f"the queue database is {reason.replace('readonly', 'read-only')}" + (f" ({detail})" if detail else ""),
            hints.get(reason, "look at the queue folder in ws's state folder"),
            EXIT_REFUSED,
        )
        self.reason = reason


class SpendUnreadable(Exception):
    """The run log could not be read while admission needed this month's spend. Not a store problem: the caller reports it."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(cause.strerror or type(cause).__name__)
        self.cause = cause


class LimitsError(WsError):
    def __init__(self, key: str, why: str) -> None:
        super().__init__("bad_config", f"[roles.queue] {key} {why}", f"fix {key} in the profile, or remove it", 2)


@dataclass(frozen=True)
class Limits:
    max_concurrent: int = MAX_CONCURRENT
    ui_slots: int = UI_SLOTS
    max_open_turns: int = MAX_OPEN_TURNS
    max_open_seconds: float = MAX_OPEN_SECONDS
    default_turns: int = DEFAULT_TURNS
    reserve_pct: float = RESERVE_PCT
    join_headroom_gb: float = JOIN_HEADROOM_GB
    ramp_s: float = RAMP_S
    busy_s: float = BUSY_S
    poll_s: float = POLL_S

    @classmethod
    def from_profile(cls, prof: dict | None) -> Limits:
        """The limits with `[roles.queue]` laid over the defaults. Raises LimitsError naming the key."""
        roles = (prof or {}).get("roles")
        table = roles.get("queue") if isinstance(roles, dict) else None
        if table is None:
            return cls()
        if not isinstance(table, dict):
            raise LimitsError("(table)", "must be a table")
        out: dict = {}
        for key, v in table.items():
            if key not in _LIMIT_KEYS:
                raise LimitsError(key, f"is not a setting (known: {', '.join(_LIMIT_KEYS)})")
            kind = _LIMIT_KEYS[key][0]
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            if kind == "int":
                ok = ok and isinstance(v, int) and v >= 1
            elif kind == "num":
                ok = ok and v > 0
            elif kind == "num0":
                ok = ok and v >= 0
            elif kind == "pct":
                ok = ok and 0 <= v < 100
            if not ok:
                raise LimitsError(
                    key,
                    "must be "
                    + {
                        "int": "a whole number of 1 or more",
                        "pct": "a percentage from 0 to 99",
                        "num0": "a number of 0 or more",
                    }.get(kind, "a number above 0"),
                )
            out[key] = v if kind == "int" else float(v)
        return cls(**out)


@dataclass(frozen=True)
class Spend:
    """The month's spend from the run log: in total and by purpose (records with no purpose count in the total only)."""

    total: float = 0.0
    by_purpose: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Request:
    """What one run asks admission to reserve."""

    job: str
    run_id: str
    purpose: str | None = None
    usd: float = runrecord.UNKNOWN_RUN_USD  # the estimate; 0 reserves no money (a slot)
    explicit_usd: bool = False  # the job named budget_usd
    strict: bool = False
    cap_usd: float | None = None  # the profile's monthly cap; None skips the money counter
    purpose_cap_usd: float | None = None
    turns: int = 0
    seconds: float = 0.0
    ram_gb: float = 0.0
    lane: str = "run"
    who: str | None = None  # join | writer, for a slot request
    credential: str = "api_key"  # api_key | local | subscription
    deadline: datetime | None = None
    lease_s: float | None = None
    owner_pid: int | None = None


@dataclass(frozen=True)
class Reservation:
    run_id: str
    claim_id: int
    usd: float
    turns: int
    seconds: float
    ram_gb: float
    lane: str
    resized: bool
    max_budget_usd: float | None  # what to pass the runner as its own spend limit; None when the estimate was a guess
    month: str
    counters: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_record(self) -> dict:
        """The run record's `reservation` key."""
        return {
            "usd": self.usd,
            "turns": self.turns,
            "seconds": self.seconds,
            "ram_gb": self.ram_gb,
            "lane": self.lane,
            "resized": self.resized,
            "counters": list(self.counters),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Decision:
    """admit: `reservation` is set. wait: a transient counter is full, try again. refuse: a typed reason, never retried."""

    kind: str
    counter: str | None = None
    reason: str = ""
    hint: str = ""
    reservation: Reservation | None = None
    notes: tuple[str, ...] = ()

    @property
    def stop_reason(self) -> str:
        """The run record's `stop_reason` for a run that did not start."""
        if self.kind == "admit":
            return ""
        return "expired" if self.counter == "expired" else f"refused:{self.counter}"


# ---------------------------------------------------------------------------------------------------------------------
# The host: memory and process identity (both injectable)


def read_memory() -> tuple[float, float] | None:
    """(MemAvailable, MemTotal) in GB from /proc/meminfo, or None where the system does not report them."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    kb: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in ("MemAvailable", "MemTotal") and parts and parts[0].isdigit():
            kb[name] = int(parts[0])
    if "MemAvailable" not in kb or not kb.get("MemTotal"):
        return None
    return kb["MemAvailable"] / 1048576, kb["MemTotal"] / 1048576


def proc_probe(pid: int) -> tuple[bool | None, str | None]:
    """(exists, start time) of a process: (True, start) when it runs, (False, None) when it does not, (None, None) when
    this system cannot say. The start time is the kernel's own (clock ticks since boot on Linux, `ps` elsewhere): a pid
    that was reused by another process has a different one."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        if Path("/proc/self/stat").exists():
            return False, None
    except OSError:
        return None, None
    else:
        fields = raw.rpartition(")")[2].split()
        return (True, fields[19]) if len(fields) > 19 else (None, None)
    try:
        p = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None, None
    out = p.stdout.strip()
    if p.returncode == 0 and out:
        return True, out
    return (False, None) if p.returncode == 1 and not out else (None, None)


def owner_state(pid: int | None, start: str | None, probe: Callable = proc_probe) -> str:
    """`alive`, `gone` or `unknown` for the process a reservation names. Gone means the pid is not running, or is running
    with another start time (it was reused). Unknown means the system cannot say, or no start time was recorded to
    tell a reused pid apart: the lease decides then."""
    if not isinstance(pid, int) or pid <= 0:
        return "unknown"
    exists, now_start = probe(pid)
    if exists is None:
        return "unknown"
    if not exists:
        return "gone"
    if not start or not now_start:
        return "unknown"
    return "alive" if start == now_start else "gone"


# ---------------------------------------------------------------------------------------------------------------------
# Small conversions


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_ns(t: datetime) -> int:
    """Unix nanoseconds of an aware time (integer arithmetic: a float loses the low digits)."""
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("a time must be timezone-aware (use datetime.now(UTC))")
    return (t - _EPOCH) // timedelta(microseconds=1) * 1000


def from_ns(ns: int | None) -> datetime | None:
    return None if ns is None else _EPOCH + timedelta(microseconds=ns // 1000)


def month_of(now: datetime) -> str:
    """`YYYY-MM` of the UTC month holding `now`: the policy's budget window, and the month `runrecord` counts by."""
    return f"{policy.budget_month(now)[0]:%Y-%m}"


def _floor4(x: float) -> float:
    return math.floor(x * 10000 + 1e-9) / 10000


def _claim() -> int:
    return int.from_bytes(os.urandom(7)) | 1


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE, or the open transaction. Rolled back if the block raises."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def store_error(e: BaseException) -> StoreError:
    """A StoreError for a sqlite3 or OS error, classified by error code (`sqlite_errorcode`) or errno, not by text."""
    if isinstance(e, sqlite3.Error):
        kind = sqlqueue.store_error_kind(e)
        return StoreError(kind if kind != "other" else "unreadable", type(e).__name__)
    if isinstance(e, PermissionError) or getattr(e, "errno", None) in (30,):  # EACCES, EROFS
        return StoreError("readonly", type(e).__name__)
    if getattr(e, "errno", None) == 28:  # ENOSPC
        return StoreError("full", type(e).__name__)
    return StoreError("unreadable", type(e).__name__)


# ---------------------------------------------------------------------------------------------------------------------
# The ledger


class Ledger:
    """The queue database: reservations (receipts) and the queued work. Every clock, host reading and file the ledger
    touches is passed in, so a test needs no real time, memory or process."""

    def __init__(
        self,
        state: Path,
        *,
        limits: Limits | None = None,
        memory: Callable[[], tuple[float, float] | None] = read_memory,
        probe: Callable = proc_probe,
        rate_limits: Callable[[datetime], usage.RateLimits | None] | None = None,
        costing: tuple[float | None, float | None] = (None, None),
        recorded: Callable[[str], dict | None] | None = None,
        sink: Callable[[dict], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.state = Path(state)
        self.folder = self.state / "queue"
        self.path = self.folder / "queue.db"
        self.limits = limits or Limits()
        self.memory, self.probe, self.costing = memory, probe, costing
        self.rate_limits = rate_limits or (lambda now: usage.read_rate_limits(self.state, now))
        self.recorded, self.sink, self.clock = recorded, sink, clock
        self._conn: sqlite3.Connection | None = None
        self._engine: sqlqueue.SqlQueue | None = None

    # -- the store ----------------------------------------------------------------------------------------------

    def conn(self) -> sqlite3.Connection:
        """The open connection; opens the database (a private folder and file) and makes its tables on first use. Raises
        StoreError for a store that is locked, full, read-only or unreadable. Two processes opening a new database at
        the same moment (a switch to WAL mode does not wait on SQLite's busy timeout) take turns for up to `busy_s`."""
        if self._conn is not None:
            return self._conn
        give_up = time.monotonic() + self.limits.busy_s
        while True:
            try:
                self._conn = self._open()
                return self._conn
            except (sqlite3.Error, OSError) as e:
                if sqlqueue.store_error_kind(e) == "locked" and time.monotonic() < give_up:
                    time.sleep(0.02)
                    continue
                raise store_error(e) from e

    def _open(self) -> sqlite3.Connection:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(self.folder)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{self.folder} is not a folder")
        if st.st_mode & 0o077:
            os.chmod(self.folder, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        c = sqlite3.connect(str(self.path), isolation_level=None, timeout=self.limits.busy_s, check_same_thread=False)
        try:
            c.row_factory = sqlite3.Row
            if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
            if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                with _transaction(c):
                    for stmt in _SCHEMA.split(";"):
                        if stmt.strip():
                            c.execute(stmt)
                    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            c.close()
            raise
        return c

    def engine(self) -> sqlqueue.SqlQueue:
        """The queued-work engine on the same connection (table `work`)."""
        if self._engine is None:
            try:
                self._engine = sqlqueue.SqlQueue(self.conn(), table=WORK_TABLE, clock=lambda: to_ns(self.clock()))
            except sqlite3.Error as e:
                raise store_error(e) from e
        return self._engine

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
        self._conn = self._engine = None

    # -- admission ----------------------------------------------------------------------------------------------

    def admit(
        self, req: Request, now: datetime, spend: Callable[[], Spend] | None = None, *, dry: bool = False
    ) -> Decision:
        """Reserve everything `req` needs, or say why not, in one transaction. `now` is the one clock reading for the
        whole admission: it names the month, the lease and the deadline check. `spend` reads this month's records (it is
        called inside the transaction, so no run can settle between the read and the sum of the open reservations).
        `dry` evaluates and writes nothing. Never raises for a store problem: that is a refusal."""
        try:
            c = self.conn()
            with _transaction(c):
                if not dry:
                    self._reap(c, now)
                try:
                    month_spend = (spend or Spend)()
                except OSError as e:
                    raise SpendUnreadable(e) from e
                decision = self._evaluate(c, req, now, month_spend)
                if not dry:
                    decision = self._apply(c, req, decision, now)
                return decision
        except StoreError as e:
            return Decision("refuse", e.kind, e.message, e.hint)
        except (sqlite3.Error, OSError) as e:
            se = store_error(e)
            return Decision("refuse", se.kind, se.message, se.hint)

    def _evaluate(self, c: sqlite3.Connection, req: Request, now: datetime, spend: Spend) -> Decision:
        ns, month = to_ns(now), month_of(now)
        refusals: list[tuple[str, str, str]] = []
        waits: list[tuple[str, str, str]] = []
        notes: list[str] = []
        counters: list[str] = []
        rooms: list[tuple[str, float, str]] = []

        def one(sql: str, *args) -> float:
            return c.execute(sql, args).fetchone()[0] or 0

        if req.deadline is not None and now >= req.deadline:
            return Decision(
                "refuse", "expired", f"the deadline, {req.deadline:%Y-%m-%d %H:%M}Z, had passed before the run could start",
                "give the job a later deadline, or run it without one",
            )  # fmt: skip
        if req.credential == "subscription":
            rl = self.rate_limits(now)
            if rl is None:
                notes.append("subscription limits unknown: no fresh statusline capture")
            else:
                for name in rl.exhausted():
                    w = getattr(rl, name)
                    refusals.append(
                        (f"subscription_{name}", f"the subscription's {name.replace('_', '-')} limit is used up until {w.resets_at:%H:%M}Z",
                         "wait for the window to reset, or give the job an API key through [[secrets]]")
                    )  # fmt: skip
                counters.append("subscription")

        if one(
            "SELECT COUNT(*) FROM receipts WHERE job = ? AND status = 'running' AND run_id != ?", req.job, req.run_id
        ):
            waits.append(("job_running", f"{req.job} is already running", "wait for it to finish"))

        if req.usd > 0:
            held = "SELECT SUM(reserved_usd) FROM receipts WHERE status = 'running' AND month = ? AND run_id != ?"
            lost = "SELECT SUM(actual_usd) FROM receipts WHERE settled = 1 AND record_written = 0 AND month = ?"
            if req.cap_usd is not None:
                reserved = one(held, month, req.run_id) + one(lost, month)
                room = req.cap_usd - spend.total - reserved
                rooms.append(("profile_cap", room, _cap_reason(req.cap_usd, spend.total, reserved)))
                counters.append("profile_cap")
            if req.purpose and req.purpose_cap_usd is not None:
                by = spend.by_purpose.get(req.purpose, 0.0)
                reserved = one(held + " AND purpose = ?", month, req.run_id, req.purpose) + one(
                    lost + " AND purpose = ?", month, req.purpose
                )
                rooms.append(
                    (
                        f"purpose:{req.purpose}",
                        req.purpose_cap_usd - by - reserved,
                        _cap_reason(req.purpose_cap_usd, by, reserved, req.purpose),
                    )
                )
                counters.append(f"purpose:{req.purpose}")
            for name, room, why in rooms:
                if room <= 0:
                    refusals.append((name, why, _cap_hint(name)))

        if req.turns > 0:
            used = one("SELECT SUM(reserved_turns) FROM receipts WHERE status = 'running' AND run_id != ?", req.run_id)
            self._fit("turns", used, req.turns, self.limits.max_open_turns, "turns", refusals, waits)
            counters.append("turns")
        if req.seconds > 0:
            used = one("SELECT SUM(reserved_s) FROM receipts WHERE status = 'running' AND run_id != ?", req.run_id)
            self._fit("time", used, req.seconds, self.limits.max_open_seconds, "seconds", refusals, waits)
            counters.append("time")

        running = one("SELECT COUNT(*) FROM receipts WHERE status = 'running' AND run_id != ?", req.run_id)
        in_lane = one(
            "SELECT COUNT(*) FROM receipts WHERE status = 'running' AND lane = ? AND run_id != ?", req.lane, req.run_id
        )
        if running >= self.limits.max_concurrent:
            waits.append(("concurrency", f"{running} runs are in progress, the most at once", "wait for one to finish"))
        lane_cap = self.limits.ui_slots if req.lane == "ui" else self.limits.max_concurrent
        if in_lane >= lane_cap:
            waits.append(
                (f"lane:{req.lane}", f"the {req.lane} lane holds {in_lane} of {lane_cap}", "wait for the slot")
            )
        counters.append("concurrency")

        mem = self.memory()
        if mem is None:
            notes.append("memory not reported on this system")
        else:
            avail, total = mem
            reserve = total * self.limits.reserve_pct / 100
            headroom = self.limits.join_headroom_gb if req.who == "writer" else 0.0
            ramping = one(
                "SELECT SUM(ram_gb) FROM receipts WHERE status = 'running' AND run_id != ? AND started_at > ?",
                req.run_id, ns - int(self.limits.ramp_s * 1e9),
            )  # fmt: skip
            if req.ram_gb > total - reserve:
                refusals.append(
                    ("memory_never", f"{req.ram_gb:g} GB can never fit beside the {reserve:.1f} GB kept free on a {total:.1f} GB host",
                     "lower resources.ram_gb")
                )  # fmt: skip
            elif avail - ramping - req.ram_gb < reserve + headroom:
                waits.append(
                    ("memory", f"{avail - ramping:.1f} GB is free; {req.ram_gb:g} GB plus {reserve + headroom:.1f} GB kept free does not fit",
                     "wait for memory to come back")
                )  # fmt: skip
            counters.append("memory")

        if req.who is not None:
            mine = c.execute("SELECT queued_at FROM receipts WHERE run_id = ?", (req.run_id,)).fetchone()
            rank = (0 if req.who == "join" else 1, mine["queued_at"] if mine else ns, req.run_id)
            for o in c.execute(
                "SELECT who, queued_at, run_id FROM receipts WHERE status = 'queued' AND message_id IS NULL AND who IS NOT NULL "
                "AND lane = ? AND run_id != ? AND lease_expires_at >= ?",
                (req.lane, req.run_id, ns),
            ):
                if (0 if o["who"] == "join" else 1, o["queued_at"], o["run_id"]) < rank:
                    waits.append(
                        (
                            "queue_ahead",
                            f"a {o['who']} is waiting for the {req.lane} lane ahead of this one",
                            "wait your turn",
                        )
                    )
                    break

        if refusals:
            name, why, hint = refusals[0]
            return Decision("refuse", name, why, hint, notes=tuple(notes))
        if waits:
            name, why, hint = waits[0]
            return Decision("wait", name, why, hint, notes=tuple(notes))

        usd, resized = (req.usd, False) if req.usd > 0 else (0.0, False)
        if req.usd > 0 and rooms:
            tight = min(rooms, key=lambda r: r[1])
            if tight[1] < req.usd:
                if req.strict:
                    return Decision(
                        "refuse", tight[0], f"{tight[0]} has US${tight[1]:.4f} left, under the strict estimate of US${req.usd:g}",
                        "lower budget_usd, or take strict off the job",
                    )  # fmt: skip
                usd, resized = _floor4(tight[1]), True
                if usd < MIN_RESERVE_USD:
                    return Decision(
                        "refuse", tight[0], f"{tight[0]} has too little left to reserve", _cap_hint(tight[0])
                    )
        res = Reservation(
            run_id=req.run_id, claim_id=_claim(), usd=usd, turns=req.turns, seconds=req.seconds, ram_gb=req.ram_gb,
            lane=req.lane, resized=resized, max_budget_usd=usd if usd > 0 and (resized or req.explicit_usd) else None,
            month=month, counters=tuple(counters), notes=tuple(notes),
        )  # fmt: skip
        return Decision("admit", reservation=res, notes=tuple(notes))

    @staticmethod
    def _fit(name, used, want, limit, unit, refusals, waits) -> None:
        if want > limit:
            refusals.append(
                (
                    name,
                    f"this run alone needs {want:g} {unit}, over the {limit:g} the queue allows",
                    f"lower the job's {unit}",
                )
            )
        elif used + want > limit:
            waits.append(
                (name, f"{used:g} of {limit:g} {unit} are reserved by runs in progress", "wait for one to finish")
            )

    def _apply(self, c: sqlite3.Connection, req: Request, d: Decision, now: datetime) -> Decision:
        ns = to_ns(now)
        month = month_of(now)
        if d.kind == "admit":
            r = d.reservation
            pid = req.owner_pid or os.getpid()
            _, start = self.probe(pid)
            lease = ns + int((req.lease_s if req.lease_s is not None else req.seconds + LEASE_SLACK_S) * 1e9)
            cols = dict(  # noqa: C408
                job=req.job, purpose=req.purpose, lane=req.lane, who=req.who, status="running", month=r.month,
                started_at=ns, reserved_usd=r.usd, reserved_turns=r.turns, reserved_s=r.seconds, ram_gb=r.ram_gb,
                resized=int(r.resized), lease_expires_at=lease, owner_pid=pid, owner_start_time=start,
                claim_id=r.claim_id, note="; ".join(r.notes) or None,
                deadline=to_ns(req.deadline) if req.deadline else None,
            )  # fmt: skip
            self._upsert(c, req.run_id, ns, cols)
        elif d.kind == "refuse":
            self._upsert(
                c, req.run_id, ns,
                dict(  # noqa: C408
                    job=req.job, purpose=req.purpose, lane=req.lane, who=req.who, status="expired" if d.counter == "expired" else "refused",
                     month=month, finished_at=ns, settled=1, actual_usd=0.0, stop_reason=d.stop_reason, note=d.reason[:300]),
            )  # fmt: skip
        elif req.who is not None:  # a waiting slot: a ticket other waiters can see, kept alive by looking again
            ttl = int(max(3 * self.limits.poll_s, 30) * 1e9)
            self._upsert(
                c, req.run_id, ns,
                dict(  # noqa: C408
                    job=req.job, purpose=req.purpose, lane=req.lane, who=req.who, status="queued", month=month,
                     lease_expires_at=ns + ttl, ram_gb=req.ram_gb, note=d.reason[:300]),
            )  # fmt: skip
        return d

    @staticmethod
    def _upsert(c: sqlite3.Connection, run_id: str, ns: int, cols: dict) -> None:
        """Update the receipt `run_id` (a queued run or a waiting ticket) or make it."""
        sets = ", ".join(f"{k} = ?" for k in cols)
        cur = c.execute(
            f"UPDATE receipts SET {sets} WHERE run_id = ? AND status IN ('queued')", (*cols.values(), run_id)
        )
        if cur.rowcount == 0:
            names = ["run_id", "queued_at", *cols]
            c.execute(
                f"INSERT INTO receipts ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                (run_id, ns, *cols.values()),
            )

    # -- settling -----------------------------------------------------------------------------------------------

    def settle(
        self,
        res: Reservation,
        *,
        status: str,
        usd: float,
        tokens: dict | None,
        stop_reason: str | None,
        now: datetime,
        record_written: bool = True,
    ) -> bool:
        """Close a reservation from what the run used. True if this call settled it, False if it was already settled
        (nothing changes the second time). Raises `sqlqueue.StaleClaim` when `res` does not hold the row's claim, and
        StoreError if the store cannot be written after a few tries. Call it after the run's record is written."""
        if status not in STATUSES or status in ACTIVE:
            raise ValueError(f"cannot settle as {status!r}")
        for attempt in range(3):
            try:
                c = self.conn()
                with _transaction(c):
                    row = c.execute("SELECT claim_id, settled FROM receipts WHERE run_id = ?", (res.run_id,)).fetchone()
                    if row is None or row["claim_id"] != res.claim_id:
                        raise sqlqueue.StaleClaim(f"{res.run_id} is not reserved under this claim")
                    if row["settled"]:
                        return False
                    c.execute(
                        "UPDATE receipts SET status = ?, finished_at = ?, settled = 1, actual_usd = ?, tokens = ?, "
                        "record_written = ?, stop_reason = ? WHERE run_id = ? AND claim_id = ? AND settled = 0",
                        (
                            status,
                            to_ns(now),
                            max(0.0, float(usd)),
                            json.dumps(tokens) if tokens else None,
                            int(record_written),
                            stop_reason,
                            res.run_id,
                            res.claim_id,
                        ),
                    )
                    return True
            except (sqlite3.Error, OSError) as e:
                if attempt == 2:
                    raise store_error(e) from e
                time.sleep(0.2)
        return False  # unreachable

    # -- reaping ------------------------------------------------------------------------------------------------

    def reap(self, now: datetime) -> int:
        """Release every reservation whose owner is gone. Returns how many."""
        with _transaction(self.conn()) as c:
            return self._reap(c, now)

    def _reap(self, c: sqlite3.Connection, now: datetime) -> int:
        ns = to_ns(now)
        n = 0
        for r in c.execute("SELECT * FROM receipts WHERE status = 'running'").fetchall():
            state = owner_state(r["owner_pid"], r["owner_start_time"], self.probe)
            if state == "alive" or (state == "unknown" and (r["lease_expires_at"] or 0) >= ns):
                continue
            self._reap_one(c, r, now)
            n += 1
        c.execute(
            "UPDATE receipts SET status = 'skipped', finished_at = ?, settled = 1, actual_usd = 0, stop_reason = 'waiter_gone' "
            "WHERE status = 'queued' AND message_id IS NULL AND who IS NOT NULL AND lease_expires_at < ?",
            (ns, ns),
        )
        return n

    def _reap_one(self, c: sqlite3.Connection, r: sqlite3.Row, now: datetime) -> None:
        """A run whose owner is gone. If it got as far as writing its record, settle from that. Otherwise write the record
        it never got to (a started run counts at the fallback cost, as any run with no reported cost does)."""
        run_id, ns = r["run_id"], to_ns(now)
        unknown_run_usd, fallback = self.costing
        rec = self.recorded(run_id) if self.recorded else None
        written = True
        if rec is None:
            rec = runrecord.new(
                run_id=run_id, job=r["job"], started_at=runrecord.iso(from_ns(r["started_at"] or r["queued_at"])),
                ended_at=runrecord.iso(now), duration_ms=0, status="error", purpose=r["purpose"], stop_reason="crashed",
                error="interrupted: the process that held this run was gone before it finished", ws_version=_version(),
            )  # fmt: skip
            written = bool(self.sink(rec)) if self.sink else False
        usd = runrecord.record_cost(rec, unknown_run_usd, fallback)
        status = {"ok": "ok", "refused": "refused", "preempted": "preempted", "expired": "expired"}.get(
            rec.get("status"), "interrupted" if rec.get("stop_reason") == "crashed" else "error"
        )
        c.execute(
            "UPDATE receipts SET status = ?, finished_at = ?, settled = 1, actual_usd = ?, record_written = ?, stop_reason = ? "
            "WHERE run_id = ? AND settled = 0",
            (status, ns, usd, int(written), rec.get("stop_reason") or "crashed", run_id),
        )

    # -- queued work --------------------------------------------------------------------------------------------

    def enqueue(
        self, job: str, purpose: str | None, now: datetime, key: str | None = None, deadline: datetime | None = None
    ) -> tuple[str, str, bool]:
        """Add a run of job file `job` to the queue. Returns (run_id, message_id, replay). The same `key` again returns
        the first one and adds nothing."""
        data = json.dumps(
            {
                "job": job,
                "purpose": purpose,
                "deadline": runrecord.iso(deadline) if deadline else None,
            },
            sort_keys=True,
        )
        try:
            eng = self.engine()
            with _transaction(self.conn()) as c:
                if key is not None:
                    old = c.execute("SELECT message_id FROM work WHERE idempotency_key = ?", (key,)).fetchone()
                    if old is not None:
                        eng.put(data, idempotency_key=key)  # raises if the key holds other data
                        row = c.execute(
                            "SELECT run_id FROM receipts WHERE message_id = ? ORDER BY queued_at DESC LIMIT 1",
                            (old["message_id"],),
                        ).fetchone()
                        return (row["run_id"] if row else ""), old["message_id"], True
                msg = eng.put(data, idempotency_key=key)
                run_id = self.new_attempt(c, msg.message_id, job, purpose, now, deadline)
                return run_id, msg.message_id, False
        except sqlqueue.IdempotencyConflict as e:
            raise WsError("idempotency_conflict", str(e), "use another --key, or the same job and deadline", 2) from e
        except (sqlite3.Error, OSError) as e:
            raise store_error(e) from e

    def new_attempt(
        self,
        c: sqlite3.Connection,
        message_id: str,
        job: str,
        purpose: str | None,
        now: datetime,
        deadline: datetime | None = None,
    ) -> str:
        """A queued receipt for the next run of a message (each attempt is one receipt and one run record)."""
        run_id = runrecord.new_run_id(now)
        c.execute(
            "INSERT INTO receipts (run_id, job, purpose, status, month, queued_at, message_id, deadline) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
            (run_id, job, purpose, month_of(now), to_ns(now), message_id, to_ns(deadline) if deadline else None),
        )
        return run_id

    def cancel(self, run_id: str, now: datetime) -> str:
        """Take a queued run out of the queue. Returns the job name. Raises WsError for one that is not queued."""
        try:
            with _transaction(self.conn()) as c:
                r = c.execute("SELECT * FROM receipts WHERE run_id = ?", (run_id,)).fetchone()
                if r is None:
                    raise WsError(
                        "not_found",
                        f"no run {run_id!r} in the queue",
                        "`ws queue list --all` shows what there is",
                        EXIT_FAILED,
                    )
                if r["status"] != "queued" or r["message_id"] is None:
                    raise WsError(
                        "not_cancellable", f"{run_id} is {r['status']}, not waiting in the queue",
                        "a running job is stopped with SIGTERM to its `ws run`; ws then settles it", EXIT_FAILED,
                    )  # fmt: skip
                self.engine().discard(r["message_id"])
                c.execute(
                    "UPDATE receipts SET status = 'skipped', finished_at = ?, settled = 1, actual_usd = 0, stop_reason = 'cancelled' WHERE run_id = ?",
                    (to_ns(now), run_id),
                )
                return r["job"]
        except (sqlite3.Error, OSError) as e:
            raise store_error(e) from e

    def end_queued(self, run_id: str, status: str, stop_reason: str | None, reason: str, now: datetime) -> None:
        """A queued run that never started and will not: its receipt ends as `status` (refused or error). A receipt that
        a run has already moved on is left alone."""
        if status not in ("refused", "error"):
            raise ValueError(f"a queued run cannot end as {status!r}")
        with _transaction(self.conn()) as c:
            c.execute(
                "UPDATE receipts SET status = ?, finished_at = ?, settled = 1, actual_usd = 0, stop_reason = ?, "
                "note = ? WHERE run_id = ? AND status = 'queued'",
                (status, to_ns(now), stop_reason, reason[:300], run_id),
            )

    def drop_ticket(self, run_id: str, now: datetime) -> None:
        """A waiter that gave up (timeout, Ctrl-C) leaves the line."""
        with contextlib.suppress(StoreError, sqlite3.Error), _transaction(self.conn()) as c:
            c.execute(
                "UPDATE receipts SET status = 'skipped', finished_at = ?, settled = 1, actual_usd = 0, "
                "stop_reason = 'gave_up' WHERE run_id = ? AND status = 'queued' AND message_id IS NULL",
                (to_ns(now), run_id),
            )

    def receipts(self, *, active_only: bool = False, last: int = 20) -> list[dict]:
        try:
            sql = "SELECT * FROM receipts" + (" WHERE status IN ('queued', 'running')" if active_only else "")
            return [
                _public(r) for r in self.conn().execute(sql + " ORDER BY queued_at DESC, run_id DESC LIMIT ?", (last,))
            ]
        except sqlite3.Error as e:
            raise store_error(e) from e

    def receipt(self, run_id: str) -> dict | None:
        try:
            r = self.conn().execute("SELECT * FROM receipts WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.Error as e:
            raise store_error(e) from e
        return _public(r) if r else None

    def queued_row(self, c: sqlite3.Connection, message_id: str) -> str | None:
        r = c.execute(
            "SELECT run_id FROM receipts WHERE message_id = ? AND status = 'queued' ORDER BY queued_at DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return r["run_id"] if r else None

    def recover(self, now: datetime) -> int:
        """Before a drain: reap dead owners, and put claimed work back that nothing is running (a drain that crashed
        between claiming and starting) or whose claim outlived `MESSAGE_LEASE_S`. Returns how many came back."""
        c = self.conn()
        eng = self.engine()
        with _transaction(c):
            self._reap(c, now)
            back = 0
            for m in eng.list_locked(ORPHAN_S):
                alive = c.execute(
                    "SELECT 1 FROM receipts WHERE message_id = ? AND status = 'running'", (m.message_id,)
                ).fetchone()
                if not alive:
                    eng.retry(m.message_id, m.claim_id, delay_s=0, count_try=True)
                    back += 1
            return back + eng.retry_expired(MESSAGE_LEASE_S, count_try=True)


def _version() -> str:
    from ws import __version__

    return __version__


def _public(r: sqlite3.Row) -> dict:
    """A receipt as `list` and `show` print it: the claim id never leaves the store."""
    out = {k: r[k] for k in r.keys() if k not in ("claim_id", "owner_start_time", "lease_expires_at")}  # noqa: SIM118
    for k in ("queued_at", "started_at", "finished_at", "deadline"):
        t = from_ns(out[k])
        out[k] = runrecord.iso(t) if t else None
    out["resized"] = bool(out["resized"])
    out["settled"] = bool(out["settled"])
    out["record_written"] = bool(out["record_written"])
    out["tokens"] = json.loads(out["tokens"]) if out["tokens"] else None
    return out


def _cap_reason(cap: float, spent: float, reserved: float, purpose: str | None = None) -> str:
    who = f"purpose {purpose}: " if purpose else ""
    if reserved > 0:
        return f"{who}this month's spend, US${spent:.4f}, plus US${reserved:.4f} reserved by runs in progress, is at the monthly cap of US${cap:g}"
    return f"{who}this month's spend, US${spent:.4f}, is at the monthly cap of US${cap:g}"


def _cap_hint(counter: str) -> str:
    if counter.startswith("purpose:"):
        return f"raise [budget.purposes] {counter.split(':', 1)[1]} in policy.toml, or wait for the next UTC month"
    return "raise monthly_cap_usd in [roles.budget] of the profile, or wait for the next UTC month"


# ---------------------------------------------------------------------------------------------------------------------
# Requests, credentials and retry classes


def credential_kind(job: jobs.Job) -> str:
    """`api_key` when the job leases a key through `[[secrets]]`, `local` when its provider is on this machine, else
    `subscription`: the runner would use whatever login it is signed in with."""
    if job.secrets:
        return "api_key"
    provider = job.runner_options.get("provider") if isinstance(job.runner_options.get("provider"), dict) else {}
    options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
    base = options.get("baseURL") or options.get("base_url")
    if isinstance(base, str):
        from urllib.parse import urlsplit

        with contextlib.suppress(ValueError):
            if (urlsplit(base).hostname or "").lower() in ("127.0.0.1", "localhost", "::1"):
                return "local"
    return "subscription"


def request_for(
    job: jobs.Job,
    run_id: str,
    *,
    cap_usd: float | None,
    unknown_run_usd: float | None,
    purpose_cap_usd: float | None,
    limits: Limits,
) -> Request:
    return Request(
        job=job.name, run_id=run_id, purpose=job.purpose, usd=float(job.budget_usd or unknown_run_usd or runrecord.UNKNOWN_RUN_USD),
        explicit_usd=job.budget_usd is not None, strict=job.strict, cap_usd=cap_usd, purpose_cap_usd=purpose_cap_usd,
        turns=int(job.max_turns or limits.default_turns), seconds=float(job.timeout_s), ram_gb=job.resources.ram_gb,
        lane=job.resources.slot, credential=credential_kind(job), deadline=job.deadline,
    )  # fmt: skip


def backoff_s(n: int) -> int:
    """Seconds to wait before retry number `n` (1 is the first): 30 s, 1 m, 5 m, 15 m, then 60 m."""
    return BACKOFF_S[min(max(n, 1), len(BACKOFF_S)) - 1]


@dataclass(frozen=True)
class Outcome:
    """What one attempt came to: the exit code of `ws run`, the record's status and stop reason, and the counter that
    made it wait (it never started)."""

    code: int
    status: str | None = None
    stop_reason: str | None = None
    waited: str | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # done | fail | retry | requeue | wait
    delay_s: float = 0
    count_try: bool = False
    why: str = ""


def classify(out: Outcome, *, tries: int, preemptible: bool, deadline: datetime | None, now: datetime) -> Action:
    """What the queue does with a message after an attempt. Exit 3 (refused, expired) is never retried; 143 is preempted
    and requeued if the job is preemptible and its deadline allows; a transient stop backs off, three tries; the rest
    are permanent. Decided from the exit code and the typed stop reason, never from message text."""
    if out.waited:
        return Action("wait", WAIT_RETRY_S, False, f"waiting for {out.waited}")
    if out.code == 0:
        return Action("done")
    if out.code in (EXIT_REFUSED, EXIT_BAD_JOB) or out.status in ("refused", "expired"):
        return Action("fail", why="refused runs are never retried")
    if out.status == "preempted" or out.stop_reason == "preempted":
        if preemptible and not _late(deadline, now, PREEMPT_REQUEUE_S):
            return Action("requeue", PREEMPT_REQUEUE_S, False, "preempted")
        return Action(
            "fail",
            why="preempted, and not requeued" + (" (past its deadline)" if preemptible else " (not preemptible)"),
        )
    if out.stop_reason in TRANSIENT_STOPS and tries < MAX_RETRIES:
        delay = backoff_s(tries + 1)
        if _late(deadline, now, delay):
            return Action("fail", why="the deadline would pass before the retry")
        return Action("retry", delay, True, f"{out.stop_reason}; retry {tries + 1} of {MAX_RETRIES}")
    return Action("fail", why="permanent" if out.stop_reason not in TRANSIENT_STOPS else "out of retries")


def _late(deadline: datetime | None, now: datetime, delay_s: float) -> bool:
    return deadline is not None and now + timedelta(seconds=delay_s) >= deadline


@dataclass
class Item:
    """One claimed message, as the runner sees it."""

    message: sqlqueue.Message
    job: str
    purpose: str | None
    deadline: datetime | None
    run_id: str
    ledger: Ledger | None = None


def drain(
    ledger: Ledger,
    runner: Callable[[Item], Outcome],
    *,
    limit: int | None = None,
    preemptible: Callable[[str], bool] = lambda job: False,
) -> list[dict]:
    """Run what is queued, one at a time: claim, run through `runner(item) -> Outcome`, then finish the claim by
    the class `classify` gives the outcome. Returns one dict per attempt. `runner` is `queue.run_item` in `ws queue run`
    and a stand-in in tests."""
    done: list[dict] = []
    ledger.recover(ledger.clock())
    eng = ledger.engine()
    while limit is None or len(done) < limit:
        msg = eng.pop()
        if msg is None:
            break
        now = ledger.clock()
        data = json.loads(msg.data)
        deadline = _parse_deadline(data.get("deadline"))
        with _transaction(ledger.conn()) as c:
            run_id = ledger.queued_row(c, msg.message_id) or ledger.new_attempt(
                c, msg.message_id, data["job"], data.get("purpose"), now, deadline
            )
        item = Item(msg, data["job"], data.get("purpose"), deadline, run_id, ledger)
        outcome = runner(item)
        now = ledger.clock()
        act = classify(outcome, tries=msg.tries, preemptible=preemptible(item.job), deadline=deadline, now=now)
        with _transaction(ledger.conn()) as c:
            if act.kind == "done":
                eng.done(msg.message_id, msg.claim_id)
            elif act.kind == "fail":
                eng.mark_failed(msg.message_id, msg.claim_id)
                refused = outcome.status in ("refused", "expired")  # (a run that did start has settled its own receipt)
                ledger.end_queued(
                    run_id, "refused" if refused else "error", outcome.stop_reason, act.why or "failed", now
                )
            else:
                eng.retry(msg.message_id, msg.claim_id, delay_s=act.delay_s, count_try=act.count_try)
                if act.kind != "wait":
                    ledger.new_attempt(c, msg.message_id, item.job, item.purpose, now, deadline)
        done.append(
            {
                "run_id": run_id,
                "job": item.job,
                "action": act.kind,
                "code": outcome.code,
                "status": outcome.status,
                "why": act.why,
            }
        )
    return done


def _parse_deadline(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    with contextlib.suppress(ValueError):
        t = datetime.fromisoformat(v)
        return t.astimezone(UTC) if t.tzinfo else None
    return None


def run_item(item: Item) -> Outcome:
    """Run one claimed message through `ws run`'s own code (so it reserves, runs and settles exactly as a direct run
    does). A job that has become unusable, or would use a subscription login, ends the attempt as refused."""
    from ws import run  # here, not at the top: run imports this module

    env = os.environ
    try:
        job = jobs.load(item.job, env)
    except jobs.JobError:
        return Outcome(EXIT_BAD_JOB, "error", "bad_job")
    if credential_kind(job) == "subscription":
        print(f"ws: {item.job} would use a subscription login, which unattended work never does", file=sys.stderr)
        if item.ledger is not None:
            item.ledger.end_queued(
                item.run_id,
                "refused",
                "refused:subscription_login",
                "would use a subscription login",
                item.ledger.clock(),
            )
        return Outcome(EXIT_REFUSED, "refused", "refused:subscription_login")
    if item.deadline is not None:
        job = dataclasses.replace(job, deadline=item.deadline)
    got: list[dict] = []
    queued = run.Queued(item.run_id)
    code = run.run_job(argparse.Namespace(job=job.name, dry_run=False), job, None, got.append, queued)
    if queued.waited:
        return Outcome(code, None, None, queued.waited)
    rec = got[0] if got else {}
    return Outcome(code, rec.get("status"), rec.get("stop_reason"))


# ---------------------------------------------------------------------------------------------------------------------
# Waiting for a slot


class SlotRefused(WsError):
    def __init__(self, d: Decision) -> None:
        super().__init__("refused", d.reason, d.hint, EXIT_REFUSED, {"counter": d.counter})


class SlotTimeout(WsError):
    def __init__(self, waited_s: float, d: Decision) -> None:
        super().__init__(
            "timeout", f"no slot after {waited_s:.0f} s: {d.reason}", "try again later, or raise --timeout-s", EXIT_FAILED,
            {"counter": d.counter},
        )  # fmt: skip


def wait_for_slot(
    ledger: Ledger,
    req: Request,
    *,
    timeout_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[Decision], None] = lambda d: None,
) -> Reservation:
    """Ask for the slot until it is granted. A wait is the normal answer to anything that is merely full; only a request
    that can never fit (or a store that cannot be used) is refused. The ledger's clock tells the time, so a test moves it."""
    t0 = ledger.clock()
    told = None
    try:
        while True:
            d = ledger.admit(req, ledger.clock())
            if d.kind == "admit":
                return d.reservation  # type: ignore[return-value]
            if d.kind == "refuse":
                raise SlotRefused(d)
            waited = (ledger.clock() - t0).total_seconds()
            if timeout_s is not None and waited >= timeout_s:
                raise SlotTimeout(waited, d)
            if d.counter != told:
                told = d.counter
                notify(d)
            sleep(
                ledger.limits.poll_s if timeout_s is None else min(ledger.limits.poll_s, max(timeout_s - waited, 0.01))
            )
    except BaseException:
        ledger.drop_ticket(req.run_id, ledger.clock())
        raise


# ---------------------------------------------------------------------------------------------------------------------
# Commands


def ledger_from_profile(prof: dict, state: Path, log: Path) -> Ledger:
    """The ledger for this machine: limits from `[roles.queue]`, costing from `[roles.budget]`, the real memory, the
    real process table, and `log` (the run log) as the place a reaped run's record is looked for and written. Raises
    LimitsError for a bad `[roles.queue]` key."""
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    budget = roles.get("budget") if isinstance(roles.get("budget"), dict) else {}

    def num(key: str) -> float | None:
        v = budget.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < math.inf else None

    return Ledger(
        state,
        limits=Limits.from_profile(prof),
        costing=(num("unknown_run_usd"), num("fallback_usd_per_mtok")),
        recorded=lambda rid: next((r for r in runrecord.read(log) if r.get("run_id") == rid), None),
        sink=lambda rec: _write_record(log, rec),
    )


def ledger_for_env(env=None) -> Ledger:
    env = os.environ if env is None else env
    try:
        prof = load(profile_path(env)) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), "fix the profile", 2) from e
    return ledger_from_profile(prof, paths.state_dir(env), runrecord.runs_path(env))


def _write_record(log: Path, rec: dict) -> bool:
    try:
        runrecord.append(log, rec)
    except OSError:
        return False
    return True


def _refuse_in_session(verb: str) -> None:
    if os.environ.get(approvals.ENV_MARKER):
        raise WsError(
            "refused", f"ws {verb} is not for a gated session ({approvals.ENV_MARKER} is set here)",
            "run it yourself, in your own terminal or from a timer", EXIT_REFUSED,
        )  # fmt: skip


def _bad_job(message: str) -> WsError:
    return WsError("bad_job", message, "fix the job file", EXIT_BAD_JOB)


def cmd_add(args: argparse.Namespace) -> int:
    _refuse_in_session("queue add")
    now = datetime.now(UTC)
    try:
        job = jobs.load(args.job)
    except jobs.JobError as e:
        raise _bad_job(str(e)) from e
    try:
        pol = policy.load()
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), "fix policy.toml", 2) from e
    if job.purpose is not None and job.purpose not in pol.budget_purposes:
        raise _bad_job(
            f"job {job.name}: purpose {job.purpose!r} is not in [budget.purposes] of the policy "
            f"(known: {', '.join(sorted(pol.budget_purposes)) or 'none'})"
        )
    if credential_kind(job) == "subscription":
        raise WsError(
            "subscription_login",
            f"{job.name} would use a subscription login, and unattended work never does",
            "give the job an API key through [[secrets]], or a local provider in runner_options",
            EXIT_REFUSED,
        )
    try:
        deadline = jobs.parse_deadline(args.deadline, "--deadline") if args.deadline else None
    except jobs.JobError as e:
        raise WsError("bad_input", str(e), "for example --deadline 2026-11-10T09:00:00Z", 2) from e
    ledger = ledger_for_env()
    run_id, message_id, replay = ledger.enqueue(job.name, job.purpose, now, args.key, deadline)
    if args.json:
        output.result({"run_id": run_id, "job": job.name, "message_id": message_id, "replay": replay})
    else:
        print(f"ws: {'already queued as' if replay else 'queued'} {run_id} ({job.name})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = ledger_for_env().receipts(active_only=not args.all, last=args.last)
    if args.json:
        output.result(rows)
        return 0
    if not rows:
        print("ws: nothing queued" if not args.all else "ws: no receipts yet")
        return 0
    for r in rows:
        usd = r["actual_usd"] if r["settled"] else r["reserved_usd"]
        print(
            f"{r['run_id']}  {r['status']:<11} {r['job']}  {r['purpose'] or '-'}  {r['lane']}  US${usd or 0:.4f}  {r['stop_reason'] or ''}".rstrip()
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    r = ledger_for_env().receipt(args.run_id)
    if r is None:
        raise WsError("not_found", f"no receipt {args.run_id!r}", "`ws queue list --all` shows them", EXIT_FAILED)
    if args.json:
        output.result(r)
    else:
        for k, v in r.items():
            print(f"{k:<16}{'-' if v is None else v}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _refuse_in_session("queue cancel")
    job = ledger_for_env().cancel(args.run_id, datetime.now(UTC))
    if args.json:
        output.result({"run_id": args.run_id, "job": job, "status": "skipped"})
    else:
        print(f"ws: cancelled {args.run_id} ({job})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _refuse_in_session("queue run")
    ledger = ledger_for_env()
    from ws import run

    with run._term_becomes_exit():
        results = drain(ledger, run_item, limit=args.max, preemptible=_preemptible)
    failed = [r for r in results if r["action"] == "fail"]
    if args.json:
        output.result({"attempts": results, "failed": len(failed)})
    else:
        print(f"ws: {len(results)} attempt(s), {len(failed)} failed")
        for r in results:
            print(f"  {r['run_id']}  {r['job']}  {r['action']}" + (f"  ({r['why']})" if r["why"] else ""))
    return EXIT_FAILED if failed else 0


def _preemptible(name: str) -> bool:
    try:
        return jobs.load(name).preemptible
    except jobs.JobError:
        return False


def cmd_slot(args: argparse.Namespace) -> int:
    cmd = list(args.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise WsError(
            "usage", "give the command to run after --", "ws queue slot --ram-gb 4 --lane ui --who join -- pytest", 2
        )
    from ws import run

    ledger = ledger_for_env()
    now = ledger.clock()
    run_id = runrecord.new_run_id(now)
    req = Request(
        job=f"slot-{args.lane}-{run_id[-6:]}", run_id=run_id, usd=0.0, ram_gb=args.ram_gb, lane=args.lane, who=args.who,
        credential="local", lease_s=SLOT_LEASE_S,
    )  # fmt: skip
    code = EXIT_FAILED
    with run._term_becomes_exit():
        res = wait_for_slot(
            ledger, req, timeout_s=args.timeout_s,
            notify=lambda d: print(f"ws: waiting for a {args.lane} slot: {d.reason}", file=sys.stderr),
        )  # fmt: skip
        status, stop = "interrupted", "interrupted"
        try:
            code = subprocess.run(cmd, check=False).returncode
            status, stop = ("ok", "completed") if code == 0 else ("error", "command_failed")
        except OSError as e:
            code, status, stop = 127, "error", "not_found"
            print(f"ws: cannot run {cmd[0]}: {e.strerror or type(e).__name__}", file=sys.stderr)
        except SystemExit as e:  # SIGTERM to `ws queue slot`
            status, stop = "preempted", "preempted"
            code = e.code if isinstance(e.code, int) else EXIT_FAILED
            raise
        finally:
            with contextlib.suppress(WsError, sqlqueue.StaleClaim):
                ledger.settle(res, status=status, usd=0.0, tokens=None, stop_reason=stop, now=ledger.clock())
    return 128 - code if code < 0 else code  # a command killed by signal N exits 128+N


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _non_negative(text: str) -> float:
    x = float(text)
    if not math.isfinite(x) or x < 0:
        raise argparse.ArgumentTypeError("must be a number of 0 or more")
    return x


def register(sub) -> None:
    q = sub.add_parser("queue", help="the job queue: reserve before a run, settle from what it used")
    qs = q.add_subparsers(dest="queue_command", required=True)

    a = qs.add_parser(
        "add", help="queue a run of a job file (unattended work: it needs an API key or a local provider)"
    )
    a.add_argument("job", help="the job name: jobs/<name>.toml in the config folder")
    a.add_argument("--key", metavar="K", help="an idempotency key: adding the same key again queues nothing new")
    a.add_argument(
        "--deadline", metavar="T", help="a UTC time such as 2026-11-10T09:00:00Z; later than that it expires"
    )
    a.set_defaults(func=cmd_add)

    ls = qs.add_parser("list", help="the queue: what is waiting or running (--all for finished ones too)")
    ls.add_argument("--all", action="store_true", help="include finished receipts")
    ls.add_argument("--last", type=_positive_int, default=20, metavar="N", help="how many (default 20)")
    ls.set_defaults(func=cmd_list)

    sh = qs.add_parser("show", help="one receipt")
    sh.add_argument("run_id")
    sh.set_defaults(func=cmd_show)

    c = qs.add_parser("cancel", help="take a queued run out of the queue")
    c.add_argument("run_id")
    c.set_defaults(func=cmd_cancel)

    r = qs.add_parser("run", help="run what is queued, one after another (what the scheduler runs)")
    r.add_argument(
        "--max", type=_positive_int, metavar="N", help="stop after N attempts (default: until the queue is empty)"
    )
    r.set_defaults(func=cmd_run)

    s = qs.add_parser(
        "slot",
        help="wait for a slot, run a command in it, give the slot back (raw: the command's own output and exit code)",
    )
    s.add_argument("--ram-gb", type=_non_negative, default=0.0, metavar="N", help="memory the command will use, in GB")
    s.add_argument("--lane", default="ui", metavar="NAME", help="the lane to take a slot in (default ui; ui holds one)")
    s.add_argument(
        "--who", choices=("join", "writer"), default="writer", help="a join goes before any writer (default writer)"
    )
    s.add_argument("--timeout-s", type=_non_negative, default=None, metavar="S", help="give up waiting after S seconds")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="-- the command to run")
    s.set_defaults(func=cmd_slot, raw=True)
