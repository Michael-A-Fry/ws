"""`ws watch`: the failure alert a scheduled unit raises, the dead-man check for jobs that stopped running, and the drop-ins
that give a unit ws does not own the same hook.

    ws watch alert UNIT        what `ws-alert@.service` runs when UNIT fails (`OnFailure=ws-alert@%n.service`): one line
                               in `<state>/logs/alerts.jsonl` (kind "not-automatic") and one ntfy line through the notify
                               role. Both carry the job name, the unit and the exit status, never a log line: a walled
                               job's log must not leave the box. A drain unit (`command = "queue"`) reaps first: when
                               its kill cut off queued runs, the queue's notice, which names them, is the one push.
    ws watch deadman           for each scheduled job with an `expected_rate`, raise one alert when no run record is
                               newer than twice that rate. Exit 1 while any job is overdue. Run hourly by
                               `ws-watch-deadman.timer`, a unit outside the jobs it watches.
    ws watch rotate JOB        rotate the job's log file if it is over the cap (`ExecStopPost=` of a job's service runs it,
                               so a job that runs a ws command rotates too, not only `ws run`).
    ws watch dropin UNIT... --out DIR
                               write `DIR/UNIT.service.d/10-ws-alert.conf` (OnFailure= only) for units ws did not write.

An overdue job raises one alert for its last run; the next one comes only after the job has run again and gone overdue
again, so a job that stays red is one alert, not a flood (a failed run is one alert from `alert`, per run).

The gap: this runs on the machine it watches, so it cannot see that machine down. An off-box check is a separate
follow-up. No `[roles.notify]` is a not-automatic line only: the alert cannot leave the box, and `ws doctor` says so.

Swap note: the push goes through `ws.roles.ntfy.send`, so another notify adapter replaces it there; the unit side is
plain systemd `OnFailure=`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import joblog, jobs, output, paths, runrecord
from ws.output import WsError

COMMAND = "watch"
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]{1,200}$")
GRACE = 2.0  # a job is overdue when its last run is older than this many expected gaps
DROPIN = "10-ws-alert.conf"
SCHEMA_VERSION = 1  # of a line in alerts.jsonl
ALERT_UNIT = "ws-alert@%n.service"
EXIT_OVERDUE = (
    3  # `ws watch deadman` found a job overdue and alerted: the unit counts it a success (SuccessExitStatus=3)
)


def alerts_path(env=None) -> Path:
    return paths.state_dir(env) / "logs" / "alerts.jsonl"


def job_of(unit: str) -> str:
    """The job a unit belongs to: `ws-daily.service` is `daily`; any other unit is its own name."""
    stem = unit.removesuffix(".service")
    return stem.removeprefix("ws-") if unit.startswith("ws-") else stem


def unit_status(unit: str) -> tuple[str, str]:
    """(Result, ExecMainStatus) of a unit from systemd, or ("unknown", "?") when it cannot be read."""
    try:
        p = subprocess.run(
            ["systemctl", "--user", "show", "-p", "Result", "-p", "ExecMainStatus", unit],
            capture_output=True, text=True, timeout=15, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "unknown", "?"
    got = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    return got.get("Result", "unknown") or "unknown", got.get("ExecMainStatus", "?") or "?"


def _write_line(env, rec: dict) -> None:
    path = alerts_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(
        {"schema": SCHEMA_VERSION, "ts": runrecord.iso(datetime.now(UTC)), "kind": "not-automatic", **rec}
    )
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _notify_cfg(env) -> dict | None:
    from ws.profile import load, profile_path
    from ws.roles import ntfy

    roles = (load(profile_path(env)) or {}).get("roles")
    cfg = roles.get("notify") if isinstance(roles, dict) else None
    return cfg if isinstance(cfg, dict) and cfg.get("adapter") == ntfy.NAME else None


def push(env, text: str, plain: str, sender=None) -> bool | None:
    """One ntfy line through the notify role (`plain` when alert_text is "plain"). True when sent, False when the send
    failed, None when there is no notify role. Never raises, because an alert must not fail a unit."""
    try:
        from ws.roles import ntfy

        cfg = _notify_cfg(env)
        if cfg is None:
            return None
        ntfy.send(cfg, env, plain if cfg.get("alert_text") == "plain" else text, "ws job", "alert", None, sender)
        return True
    except Exception:  # noqa: BLE001  NotifyError and CapReached land here: the not-automatic line already says it
        return False


def main_pid(unit: str) -> int | None:
    """The process systemd started for `unit` (ExecMainPID; it stays readable after the unit failed). None when it
    cannot be read."""
    try:
        p = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ExecMainPID", "--value", unit],
            capture_output=True, text=True, timeout=15, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        pid = int(p.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return pid if pid > 0 else None


REAPED_WINDOW = timedelta(minutes=15)  # a reaper tick that told a drain's kill before its OnFailure= alert ran


def reap_for_drain(job: str, env, pid_of_unit: Callable[[], int | None] = lambda: None) -> list[dict]:
    """For a unit that drains the queue (a `command = "queue"` job): reap now and return each queued run held by `pid`
    (the unit's own process) that a reaper told the person about, now or in the last few minutes (the reaper timer
    may have got there first): job, run_id, owner_pid, queued. The drain runs queued jobs in its own process, so when
    its unit fails because it was killed, the runs it held are gone too, and the reaper's notice names them. Empty for
    any other job, with no pid, and when the queue cannot be read (then the unit's own alert is the notice)."""
    from ws import queue  # here, not at the top: queue reads this module's alerts file

    if job not in queue.drain_jobs(env):
        return []
    pid = pid_of_unit()  # asked only for a drain: it is a systemctl call
    if pid is None:
        return []
    try:
        ledger = queue.ledger_for_env(env)
        try:
            now = ledger.clock()
            told = [t for t in ledger.reap_report(now)[1] if t.get("owner_pid") == pid and t.get("queued")]
            seen = {t["run_id"] for t in told}
            return told + [t for t in ledger.reaped_of(pid, now - REAPED_WINDOW) if t["run_id"] not in seen]
        finally:
            ledger.close()
    except Exception:  # noqa: BLE001  an alert must not fail: the unit's own push goes instead
        return []


def alert_unit(
    unit: str,
    env=None,
    status: Callable[[str], tuple[str, str]] = unit_status,
    sender=None,
    job: str | None = None,
    reap: Callable[..., list[dict]] = reap_for_drain,
    pid_of: Callable[[str], int | None] = main_pid,
) -> dict:
    """Record and push one failed-unit alert. Returns what was written. For a drain unit whose kill cut off a queued run
    it was running itself (reaped now, its owner the unit's own process), the reaper's notice, which names that job,
    stands in for the push: one notice per kill, naming the job that was cut off, not the drain. Any other failure of
    the unit (a queued job that exited non-zero, or runs some other process held) is pushed as usual. The line is
    written either way, with what was reaped."""
    env = os.environ if env is None else env
    if not UNIT_RE.match(unit):
        raise ValueError(f"{unit!r} is not a unit name")
    job = job or job_of(unit)
    result, code = status(unit)
    rec = {"event": "unit-failed", "job": job, "unit": unit, "result": result, "exit_status": code}
    mine = reap(job, env, lambda: pid_of(unit))
    if mine:
        rec["reaped"] = [{"job": t["job"], "run_id": t["run_id"]} for t in mine]
        _write_line(env, {**rec, "pushed": False})
        return {**rec, "pushed": False}
    sent = push(env, f"{job} failed: {unit} {result}, exit {code}", "a job failed", sender)
    _write_line(env, {**rec, "pushed": sent is True})
    return {**rec, "pushed": sent is True}


# ---- the dead-man check ---------------------------------------------------------------------------------------------


def _when(text: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def last_runs(env) -> dict[str, datetime]:
    """job -> the start time of its newest run record (any status: a failed run is the failure alert's business)."""
    out: dict[str, datetime] = {}
    for rec in runrecord.read(runrecord.runs_path(env)):
        t, name = _when(rec.get("started_at")), rec.get("job")
        if t and isinstance(name, str) and (name not in out or t > out[name]):
            out[name] = t
    return out


def overdue(env, now: datetime) -> list[dict]:
    """Each scheduled job with an expected_rate whose last run is older than GRACE gaps. A job that never ran counts from
    its file's modified time."""
    last, found = last_runs(env), []
    for s in jobs.scheduled(env):
        if s.expected_rate_s is None or not s.records:
            continue  # a job that runs a ws command leaves no run record under its name: nothing to measure
        since = last.get(s.name)
        if since is None:
            try:
                since = datetime.fromtimestamp((jobs.jobs_dir(env) / f"{s.name}.toml").stat().st_mtime, UTC)
            except OSError:
                continue
        age = (now - since).total_seconds()
        if age > GRACE * s.expected_rate_s:
            found.append(
                {"job": s.name, "last_run": last.get(s.name) and runrecord.iso(last[s.name]),
                 "hours_since": round(age / 3600, 1), "limit_hours": round(GRACE * s.expected_rate_s / 3600, 1)}
            )  # fmt: skip
    return found


def _state_path(env) -> Path:
    return paths.state_dir(env) / "logs" / "deadman.json"


def deadman(env=None, now: datetime | None = None, sender=None) -> list[dict]:
    """Check every job; one alert per overdue job per last run (not one per check). Returns the overdue jobs."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    misses = overdue(env, now)
    try:
        told = json.loads(_state_path(env).read_text(encoding="utf-8"))
        told = told if isinstance(told, dict) else {}
    except (OSError, ValueError):
        told = {}
    nowtold = {}
    for m in misses:
        key = m["last_run"] or "never"
        if told.get(m["job"]) == key:
            nowtold[m["job"]] = key
            continue  # already raised for this last run
        text = f"{m['job']} has not run in {m['hours_since']} h (limit {m['limit_hours']} h)"
        sent = push(env, text, "a job has not run", sender)
        _write_line(env, {"event": "no-run", "job": m["job"], "hours_since": m["hours_since"], "pushed": sent is True})
        if sent is not False:  # a failed send is tried again at the next check; no role at all is not retried
            nowtold[m["job"]] = key
    state = _state_path(env)
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_text(
        json.dumps(nowtold), encoding="utf-8"
    )  # a job back in time drops out, so the next miss alerts again
    return misses


# ---- drop-ins for units ws did not write ----------------------------------------------------------------------------------


def dropin_text() -> str:
    return f"# Written by ws: the failure hook for a unit ws did not write; `rm` this file to undo it.\n[Unit]\nOnFailure={ALERT_UNIT}\n"


def write_dropins(units: list[str], out: Path) -> list[Path]:
    """`out/<unit>.service.d/10-ws-alert.conf` for each unit (a name with or without `.service`)."""
    made = []
    for u in units:
        name = u if u.endswith(".service") else f"{u}.service"
        if not UNIT_RE.match(name) or "@" in name:
            raise ValueError(f"{u!r} is not a plain service unit name")
        f = out / f"{name}.d" / DROPIN
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(dropin_text(), encoding="utf-8")
        made.append(f)
    return made


# ---- command ----------------------------------------------------------------------------------------------------------


def cmd_alert(args: argparse.Namespace) -> int:
    try:
        rec = alert_unit(args.unit)
    except ValueError as e:
        raise WsError("invalid_unit", str(e), "pass the failed unit's name, as OnFailure= does", 2) from e
    if rec.get("reaped"):
        names = ", ".join(sorted({r["job"] for r in rec["reaped"]}))
        print(f"ws: alert for {rec['job']}: the queue's notice named the run(s) it cut off ({names})")
    else:
        print(f"ws: alert for {rec['job']} ({'pushed' if rec['pushed'] else 'not pushed: see alerts.jsonl'})")
    return 0


def cmd_deadman(args: argparse.Namespace) -> int:
    try:
        misses = deadman()
    except jobs.JobError as e:
        raise WsError("failed", str(e)) from e
    for m in misses:
        print(f"ws: {m['job']} has not run in {m['hours_since']} h (limit {m['limit_hours']} h)")
    if getattr(args, "json", False):
        output.result({"overdue": misses})
    return EXIT_OVERDUE if misses else 0


def cmd_rotate(args: argparse.Namespace) -> int:
    if not jobs.NAME.match(args.job):
        raise WsError("invalid_job", f"{args.job!r} is not a job name", "a job name is letters, digits, - and _", 2)
    log = paths.state_dir(os.environ) / "logs" / f"{args.job}.log"
    print(f"ws: {log.name}: {'rotated' if joblog.rotate(log) else 'under the cap'}")
    return 0


def cmd_dropin(args: argparse.Namespace) -> int:
    try:
        made = write_dropins(args.units, Path(args.out))
    except ValueError as e:
        raise WsError("invalid_unit", str(e), "", 2) from e
    for f in made:
        print(f)
    return 0


def register(sub) -> None:
    w = sub.add_parser("watch", help="the failure alert and dead-man check for scheduled jobs")
    ws = w.add_subparsers(dest="watch_command", required=True)
    p = ws.add_parser(
        "alert", help="what a failed unit runs: one not-automatic line and one push (job, unit, exit status)"
    )
    p.add_argument("unit")
    p.set_defaults(func=cmd_alert)
    p = ws.add_parser(
        "deadman", help="alert once for each job with no run record in twice its expected rate; exit 1 if any"
    )
    p.set_defaults(func=cmd_deadman)
    p = ws.add_parser("rotate", help="rotate a job's log file when it is over the size cap")
    p.add_argument("job")
    p.set_defaults(func=cmd_rotate)
    p = ws.add_parser("dropin", help="write OnFailure= drop-ins for units ws did not write")
    p.add_argument("units", nargs="+")
    p.add_argument("--out", required=True, metavar="DIR")
    p.set_defaults(func=cmd_dropin)
