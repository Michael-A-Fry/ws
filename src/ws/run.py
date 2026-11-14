"""`ws run JOB`: every headless job goes through here and leaves one complete record in `runs.jsonl`.

    ws run JOB [--dry-run] [--parent RUN_ID]
                                   run `jobs/JOB.toml` in the config folder; --dry-run shows what would start and starts nothing;
                                   --parent spends from the reservation of a run in progress
    ws runs [--last N] [--json]    the last N records (default 10)

The steps: load the job, profile and policy; refuse (exit 3, and still write a record with status "refused") when there
is no monthly cap, the Rule of Two says no, or the runner cannot price the model and the job does not say
`allow_unpriced = true`; reserve (`ws.queue`, one transaction: the monthly cap, the job's purpose cap, turns, time, a slot
and memory, and a refusal that names the counter that said no); write the runner's gate config into a per-run folder
`runs/<run_id>/gate` in the state folder (the gate denies the agent writes to `runs/` and `logs/`, so those hold only
what ws wrote); lease each secret from the broker (the runner only ever holds a placeholder key and the
lease's base URL); start the runner with a scrubbed environment, the prompt on stdin and the job's timeout; keep its
output in files in the run folder (capped); read that into the record; release the leases whatever happened, including a
Ctrl-C or a SIGTERM to `ws run`.

The money is reserved before each run and settled after it, from what the run used (`ws.queue` explains the counters).
When the room left under a cap is less than the job's estimate, the reservation shrinks to it and Claude Code is
passed that sum as `--max-budget-usd`, which does enforce it during the run; OpenCode has no such flag, so there the
reservation holds the money but only the settle, from the actuals, corrects it. A run whose cost the runner did not
report counts at the person's own fallback ([roles.budget] fallback_usd_per_mtok, else unknown_run_usd), so spend is
never undercounted. ws ships no prices. A run that ends on SIGTERM (exit 143) is `preempted`: settled at its actuals.

One recovery per known failure: a small table (`RECOVERIES`) names the failures ws can mend, each typed by the
exception class that reports it, never by its message. Today that is the broker not answering when a secret is leased:
ws starts a per-user broker once (or, for a broker it does not own, waits and asks again once). Each try is written to
the record's `recovered` list as {kind, action, ok}; if the failure comes back, the run ends as an error and says so.
The job may also declare `green`, what its verification covers; the record carries it.

A job with no `cwd` runs in a fresh empty folder, `$XDG_STATE_HOME/ws-work/<run_id>` (default `~/.local/state/...`).
It is beside ws's folders and not in one on purpose: the wall makes those read-only, so a folder there could not be
written to. The `ws-work` folder is made private (0700) and checked before use: not a link, ours, and not writable by
anyone else, so another user cannot plant one to steer the run. `ws uninstall` removes the empty run folders there and
then `ws-work` once it is empty; a run folder that holds anything stays, and so does everything above it, because what a
job wrote is the person's.

A `ws run` typed at the command line that ends refused, expired or failed sends one line through the notify role, if the
profile has one (`ws.queue.notify_stop`); a run from the queue or an eval does not, because the queue says a stop when it
gives a message up, not at each retry.

Exit: 0 ok, 1 the runner failed or timed out, 2 the job could not be read (a record is written when the job name is
safe), 3 refused (the cap or a purpose cap, the Rule of Two, an expired deadline, a store that cannot be used).
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import dataclasses
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ws import (
    __version__,
    adapters,
    broker,
    gate,
    joblock,
    joblog,
    jobs,
    jobwatch,
    jsonl,
    output,
    paths,
    policy,
    queue,
    runrecord,
    selfcmd,
)
from ws import secrets as wsecrets
from ws._vendor import sqlqueue
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path

# The only variables the runner inherits from ws. Anything else it needs is set by the adapter or the job file.
PASS_ENV = ("PATH", "HOME", "LANG", "TZ", "TERM")
EXIT_FAILED, EXIT_BAD_JOB, EXIT_REFUSED = 1, 2, 3
OUTPUT_CAP = 32 * 1024 * 1024  # bytes kept of each of stdout and stderr; the rest is read and dropped
TERM_GRACE_S = 3.0  # how long a runner has to stop after SIGTERM before its whole group gets SIGKILL
BROKER_START_S = 5.0  # how long a broker ws just started has to answer
RECOVERY_WAIT_S = 2.0  # how long to wait before asking a broker ws does not own a second time
_PLAN_LEASE = broker.Lease(lease="plan", base_url="http://127.0.0.1:0/l/PLAN", header="x-api-key", expires_at="")


def exe_version(exe: str, env: dict | None = None, timeout: float = 10.0) -> str | None:
    """What `<exe> --version` prints, as a version number (or its first line; "" if it prints nothing). None if it
    does not run: it cannot start, exits non-zero or takes longer than `timeout`."""
    try:
        p = subprocess.run(
            [exe, "--version"], env=env, capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return None
    if not out:
        return ""
    m = re.search(r"\d+(?:\.\d+)+\S*", out)
    return m.group(0) if m else out.splitlines()[0][:60]


def _number(cfg: dict, key: str) -> float | None:
    v = cfg.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < float("inf") else None


def _budget(prof: dict) -> tuple[float | None, float | None, float | None]:
    """(monthly_cap_usd, unknown_run_usd, fallback_usd_per_mtok) from [roles.budget]; None for one that is missing or
    not a positive number."""
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    cfg = roles.get("budget") if isinstance(roles.get("budget"), dict) else {}
    return _number(cfg, "monthly_cap_usd"), _number(cfg, "unknown_run_usd"), _number(cfg, "fallback_usd_per_mtok")


def _start_broker(sock: Path) -> bool:
    """Start a per-user broker on `sock` (it clears a stale socket file itself) and wait for it to answer.

    It runs isolated (`selfcmd.argv`), in ws's state folder and with the runner's scrubbed environment plus the layout, never the
    project folder's `ws/`. It is left running on purpose, so the next run finds it, in its own session; its stderr goes
    to `logs/broker-start.log`. Started from a systemd oneshot unit (a timer's job), the unit's kill mode ends it with
    the unit; that is fine, the next run starts it again."""
    home = paths.state_dir().absolute()
    try:
        logs = home / "logs"
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        err_fd = os.open(logs / "broker-start.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            subprocess.Popen(
                [*selfcmd.argv(), "broker", "serve", "--socket", str(sock)],
                env=_child_env({}), cwd=home,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_fd, start_new_session=True,
            )  # fmt: skip
        finally:
            os.close(err_fd)
    except OSError:
        return False
    deadline = time.monotonic() + BROKER_START_S
    while time.monotonic() < deadline:
        try:
            broker.status(sock, timeout=1.0)
            return True
        except broker.BrokerError:
            time.sleep(0.1)
    return False


def _mend_broker(sock: Path, err: broker.BrokerUnavailable) -> tuple[str, bool]:
    """(what ws did, whether the broker answers now). Only a broker that is not running (no socket file, or nothing
    listening on it), on a socket in this user's own run folder, is one ws may start. Any other trouble (a timeout, no
    permission, a bad reply) or any other socket (the walled service, one the profile names) is not ws's to start, so it
    waits and looks again."""
    if err.not_running and sock == paths.state_dir().absolute() / "run" / "broker.sock":
        return "started the broker", _start_broker(sock)
    time.sleep(RECOVERY_WAIT_S)
    try:
        broker.status(sock, timeout=2.0)
    except broker.BrokerError:
        return "waited and asked again", False
    return "waited and asked again", True


# The known failures ws mends once, keyed by the exception class that reports each (a subclass counts): (kind, what to do
# about it, given the socket and the exception). A new
# row is a typed error that something in ws already raises, and a fix that is safe to try without asking.
RECOVERIES = {broker.BrokerUnavailable: ("broker_unavailable", _mend_broker)}


def _lease(s: jobs.SecretUse, sock: Path, rec: dict) -> broker.Lease:
    """Lease one secret. A failure in `RECOVERIES` is mended once per run and the lease asked for again; a second
    failure (or a failed fix) is raised as it is, and the run ends as an error."""
    try:
        return broker.request(s.name, s.purpose, s.host, socket=sock)
    except tuple(RECOVERIES) as e:
        if isinstance(e, broker.SocketPathTooLong):
            raise  # nothing to mend: starting or waiting for a broker does not shorten the path
        kind, mend = next(v for k, v in RECOVERIES.items() if isinstance(e, k))
        if any(r["kind"] == kind for r in rec["recovered"]):
            raise
        action, ok = mend(sock, e)
        rec["recovered"].append({"kind": kind, "action": action, "ok": ok})
        if not ok:
            raise
    return broker.request(s.name, s.purpose, s.host, socket=sock)


def _rule_label(v: policy.Verdict) -> str:
    return "refused" if not v.ok else f"exception:{v.exception}" if v.exception else "ok"


def _redact(text: str, leases: list[broker.Lease]) -> str:
    """Take any lease URL, or the token in it, out of text before it is stored or shown."""
    for lease in leases:
        text = text.replace(lease.base_url, "<lease>").replace(lease.base_url.rsplit("/", 1)[-1], "<lease>")
    return text


def _short(text: str, leases: list[broker.Lease], n: int = 200) -> str:
    return _redact(" ".join(str(text).split()), leases)[:n]


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update(extra)
    env.update(paths.env_for(os.environ))  # last: where the gate hook finds the policy
    return env


def work_root(env: dict[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/ws-work` (default `~/.local/state/ws-work`, `paths.work_dir`): the work folders of jobs with no
    `cwd`, one per run. `ws uninstall` removes the empty run folders in it, and the folder itself once it is empty."""
    return paths.work_dir(os.environ if env is None else env)


def default_cwd(home: Path, run_id: str, env: dict[str, str] | None = None) -> Path | None:
    """The folder a job with no `cwd` runs in: `<work root>/<run_id>`, never inside `home` (ws's state folder) or any
    other of ws's folders, which the wall makes read-only for the runner. None when the work root would sit inside one
    (a `XDG_STATE_HOME` set to a ws folder): `_make_default_cwd` then makes a private folder under the temp dir. Pure: it
    makes nothing."""
    folder = work_root(env) / run_id
    held = [home, *paths.roots(os.environ if env is None else env)]
    return None if any(folder.is_relative_to(r) for r in held) else folder


_RUN_FOLDER = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")  # runrecord.new_run_id


def _check_private(folder: Path, allowed: re.Pattern) -> None:
    """Refuse a folder that is a link, is not ours, that group or others can write to (lstat: a link is not followed), or
    that holds anything but folders whose names match `allowed`. The last one blocks a file planted by another process of
    the same user (an AGENTS.md or CLAUDE.md the runner would read from a parent of its working folder)."""
    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise WsError(
            "unsafe_folder",
            f"{folder} is not a private folder of yours (a link, someone else's, or writable by others)",
            f"look at it, and remove or fix it, or set XDG_STATE_HOME to a folder of your own: `ls -ld {folder}`",
        )
    for child in folder.iterdir():
        if not allowed.match(child.name) or child.is_symlink() or not child.is_dir():
            raise WsError(
                "unsafe_folder",
                f"{child} should not be in {folder}: only ws's own folders belong there",
                f"look at it, and move it away if it is yours, or set XDG_STATE_HOME to a folder of your own: `ls -la {folder}`",
            )


def _make_default_cwd(planned: Path | None) -> Path:
    """Make the default working folder. The folder ws owns on the way (`ws-work`) is made 0700 if missing and checked
    either way; the last one must be new (mkdir without exist_ok), so a run never starts in a folder someone else
    prepared. The state home above them is the person's own and is only created if missing."""
    if planned is None:
        return Path(tempfile.mkdtemp(prefix="ws-work-"))  # private (0700) and unpredictable
    planned.parent.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(planned.parent, 0o700)
    _check_private(planned.parent, _RUN_FOLDER)
    try:
        os.mkdir(planned, 0o700)
    except FileExistsError as e:
        raise WsError(
            "unsafe_folder", f"{planned} already exists, so ws will not run in it", f"remove it: `rm -r {planned}`"
        ) from e
    return planned


def _find_exe(name: str, env: dict[str, str]) -> str | None:
    """The absolute path of the runner executable, or None if there is none."""
    if os.sep in name:
        p = Path(name).expanduser()
        return os.path.abspath(p) if p.is_file() and os.access(p, os.X_OK) else None
    found = shutil.which(name, path=env.get("PATH"))
    return os.path.abspath(found) if found else None


def _signal_group(p: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, sig)


def _kill(p: subprocess.Popen, grace_s: float | None = None) -> None:
    """Stop the runner and everything it started: SIGTERM, a short grace (the job's `grace_s`, else `TERM_GRACE_S`), then
    SIGKILL to the whole group. The SIGKILL is sent even when the runner itself has gone, because a child that ignores
    SIGTERM outlives it."""
    _signal_group(p, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        p.wait(timeout=TERM_GRACE_S if grace_s is None else grace_s)
    _signal_group(p, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        p.wait(timeout=5)


def _pump(src, dest: Path, cap: int, truncated: list[bool]) -> None:
    """Copy a pipe to a private file, keeping at most `cap` bytes and reading (and dropping) the rest, so a runner
    that prints without end never fills memory or blocks on a full pipe."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    kept = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := src.read1(65536):
            room = cap - kept
            if len(chunk) > room:
                truncated[0] = True
            if room > 0:
                out.write(chunk[:room])
                kept += min(len(chunk), room)


def _spawn(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    timeout_s: float,
    stdin_path: Path | None,
    out_dir: Path,
    grace_s: float | None = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[int | None, bool, bool]:
    """Run the runner, its prompt from `stdin_path`, its output going to out_dir/stdout.txt and stderr.txt.
    Returns (exit code or None if killed, timed out, output truncated). A runner that overruns, or a `ws run` that is
    interrupted, kills the runner's whole process group before anything else happens. `pass_fds` stay open in the runner
    (the wall's seccomp program) and are closed here once it has started, or failed to."""
    stdin = subprocess.DEVNULL
    try:
        if stdin_path:
            stdin = open(stdin_path, "rb")  # noqa: SIM115
        p = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, pass_fds=pass_fds,
        )  # fmt: skip
    finally:
        if stdin is not subprocess.DEVNULL:
            stdin.close()
        for fd in pass_fds:
            os.close(fd)
    flags = [[False], [False]]
    threads = [
        threading.Thread(target=_pump, args=(src, out_dir / name, OUTPUT_CAP, flag), daemon=True)
        for src, name, flag in ((p.stdout, "stdout.txt", flags[0]), (p.stderr, "stderr.txt", flags[1]))
    ]
    for t in threads:
        t.start()
    code: int | None = None
    timed_out = False
    try:
        try:
            code = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(p, grace_s)
        _signal_group(p, signal.SIGKILL)  # whatever the runner left behind holds the pipes open
    except BaseException:
        _kill(p, grace_s)
        raise
    finally:
        for t in threads:
            t.join(timeout=10)
    return code, timed_out, any(f[0] for f in flags)


def _read_back(path: Path, leases: list[broker.Lease]) -> str:
    """The saved output, with any lease URL taken out (and the file rewritten if there was one)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    clean = _redact(text, leases)
    if clean != text:
        _write_private(path, clean)
    return clean


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(text)


def _base_record(job: jobs.Job, run_id: str, started: datetime, pol: policy.Policy, verdict: policy.Verdict) -> dict:
    return runrecord.new(
        run_id=run_id,
        job=job.name,
        runner=job.runner,
        model_requested=job.model,
        started_at=runrecord.iso(started),
        cwd=job.cwd and str(Path(job.cwd).expanduser()),
        policy={"dial": pol.dial, "rule_of_two": _rule_label(verdict)},
        ws_version=__version__,
        allow_unpriced=job.allow_unpriced,
        green=job.green,
        purpose=job.purpose,
    )


def _job_class(pol: policy.Policy, job: jobs.Job) -> policy.Class | None:
    """The data class a job works in: that of the real path of its own `cwd`; a job with none runs in a fresh default
    folder that is in no class (`policy.UNCLASSIFIED`, the strictest). None when the policy has no classes."""
    if not pol.classes:
        return None
    return policy.class_of(pol, os.path.realpath(Path(job.cwd).expanduser())) if job.cwd else policy.UNCLASSIFIED


def _wall_problem(pol: policy.Policy, job: jobs.Job, adapter, cwd: Path) -> str | None:
    """Words for why the wall can not be put round a job, or None: the checks a `ws gate launch` makes before it starts a
    runner, made for the job's `cwd` and not this process's. The account must fit the job's class (a class account runs
    jobs in its class's folders only, and the default folder is in none); and with the sandbox on, the folder must be a
    project folder (not the home folder or above it), and for a walled class its launch file must be in place and the
    project must hold nothing the class does not allow (Claude Code: `wallsettings.project_problem`; a runner that ws
    puts in its own wall, OpenCode: the adapter's `project_problem`). Static, so `--dry-run` lists it as a refusal."""
    from ws import sandbox, wallsettings

    cls = _job_class(pol, job)
    try:
        refusal = sandbox.class_user_refusal(pol, cls)
        if refusal is None and pol.gate.sandbox and job.cwd:
            project = sandbox.project_folder(str(cwd), os.environ)
            check = (
                wallsettings.project_problem
                if hasattr(adapter, "for_project")
                else getattr(adapter, "project_problem", None)
            )
            if check and sandbox.class_launch_doc(cls) is not None:
                refusal = check([project], paths.home_dir())
    except sandbox.SandboxError as e:
        return f"refusing to start {job.runner}: {e}"
    except WsError as e:
        refusal = e
    return f"{refusal.message}; {refusal.hint}" if refusal and refusal.hint else (refusal.message if refusal else None)


def _egress(pol: policy.Policy, job: jobs.Job):
    """(plan, refusal) for the data class a run works in: the class of the job's own `cwd` (a job with none runs in a
    fresh default folder that is in no class, so it gets the strictest), G7 (the model provider the class allows) and
    the network list the run gets. The plan is None when the policy has no classes: the network is as it was. A refusal
    is words that name the class only."""
    from ws import sandbox  # late: only a policy with classes needs it

    cls = _job_class(pol, job)
    provider = sandbox.job_provider(job.runner, job.model, job.runner_options)
    why = sandbox.provider_problem(cls, provider)
    if why:
        return None, why
    plan = sandbox.egress_plan(pol, cls, provider)
    if plan is not None and not pol.gate.sandbox:
        return None, f"class {plan.cls!r}: its network list is applied by the sandbox, and [gate] sandbox is off"
    return plan, None


def _refusal(prof: dict, verdict: policy.Verdict, job: jobs.Job, adapter) -> tuple[str, str] | None:
    """(counter, reason) for the refusals that need no look at the money: no cap set, the Rule of Two, a model the runner
    cannot price. What the cap and the purpose cap say is admission's (`queue.Ledger.admit`)."""
    if _budget(prof)[0] is None:
        return "profile_cap", "no monthly budget cap (WS103): set monthly_cap_usd in [roles.budget] of the profile"
    if not verdict.ok:
        return "rule_of_two", verdict.reason
    if not job.allow_unpriced and not getattr(adapter, "priced", lambda _job: True)(job):
        return "unpriced", (
            f"{job.runner} cannot say what {job.model} costs, so the cap could not be kept; set allow_unpriced = true "
            "in the job to run it anyway (it then counts at your fallback price)"
        )
    return None


def _announce_stop(rec: dict) -> None:
    """For a `ws run` typed at the command line: say once, through the notify role, that the run stopped. Call it after
    the record is written and the reservation settled. A run that ended `ok`, or was cut short (`preempted`,
    `interrupted`: SIGTERM, Ctrl-C) is not announced; the person or the scheduler did that."""
    if not _says_stops.get() or rec.get("status") in (None, "ok", "preempted"):
        return
    if rec.get("stop_reason") in ("preempted", "interrupted"):
        return
    if os.environ.get("INVOCATION_ID"):
        return  # started by systemd: the unit fails and its OnFailure= alerts, so one stop is one push, not two
    if queue.notify_stop(rec["job"], rec["run_id"], rec.get("stop_reason") or rec["status"]):
        _told.append(rec["job"])


def _append(log: Path, rec: dict) -> bool:
    """Write the record. A log that cannot be written (its lock held, the disk full) gets the record in the fallback
    file instead, with a warning, and that counts as written: the spend is in `runs.fallback.jsonl`, which `read` and
    the cap check include. False (after saying why) only when the fallback fails too."""
    try:
        where = runrecord.append(log, rec)
    except OSError as e:
        print(f"ws: could not write the record to {log}: {e.strerror or type(e).__name__}", file=sys.stderr)
        return False
    if where != log:
        print(f"ws: {log.name} could not be written; the record is in {where} (`ws runs` lists it)", file=sys.stderr)
    return True


def _lock_reason(log: Path) -> str | None:
    """Why a run must be refused because the log's lock is held, else None. A session can leave a process holding the
    lock it can read; a run that went ahead would then be recorded only in the fallback, so it is refused instead."""
    try:
        runrecord.probe(log)
    except jsonl.LockTimeout:
        lock = log.with_name(log.name + ".lock")
        return (
            f"another process is holding the lock on the run log ({lock}), so this run could not be recorded; "
            f"`fuser {lock}` or `lsof {lock}` shows which, stop it and run again"
        )
    except OSError:
        return None  # a log that cannot be opened at all is reported by the read below
    return None


def _refuse(rec: dict, reason: str, path: Path, stop_reason: str, status: str = "refused") -> int:
    rec.update(status=status, error=reason[:300], ended_at=rec["started_at"], duration_ms=0, stop_reason=stop_reason)
    _append(path, rec)
    print(f"ws: refused: {reason}", file=sys.stderr)
    print(f"ws: refusal recorded as {rec['run_id']}", file=sys.stderr)
    _announce_stop(rec)
    return EXIT_REFUSED


def _bad_job(name: str, message: str, log: Path, job: jobs.Job | None = None, record: bool = True) -> int:
    """A job that cannot run still leaves a record (status "error"), when its name is safe to write down."""
    print(f"ws: {message}", file=sys.stderr)
    if not record:
        return EXIT_BAD_JOB
    if not jobs.NAME.match(name):
        print(f"ws: no record: {name!r} is not a safe job name", file=sys.stderr)
        return EXIT_BAD_JOB
    now = datetime.now(UTC)
    rec = runrecord.new(
        run_id=runrecord.new_run_id(now),
        job=name,
        runner=job.runner if job else None,
        model_requested=job.model if job else None,
        started_at=runrecord.iso(now),
        ended_at=runrecord.iso(now),
        duration_ms=0,
        status="error",
        error=_short(message, []),
        ws_version=__version__,
        allow_unpriced=bool(job and job.allow_unpriced),
        green=job.green if job else None,
    )
    if _append(log, rec):
        print(f"ws: recorded as {rec['run_id']}", file=sys.stderr)
    _announce_stop(rec)
    return EXIT_BAD_JOB


def _merge(rec: dict, part: dict) -> None:
    for k, v in part.items():
        if k in runrecord.KEYS and v is not None:
            rec[k] = v
    rec["tokens"] = {**runrecord.blank_tokens(), **(rec["tokens"] or {})}


def _on_term(signum, frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _term_becomes_exit():
    """While `ws run` works, SIGTERM raises SystemExit, so the finally blocks run: the runner is killed, the leases are
    released and the record is written. (Only the main thread can install a handler.)"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.signal(signal.SIGTERM, _on_term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


@dataclass
class Queued:
    """A run that comes from the queue: `run_id` is the receipt that waits for it. `waited` is set when admission said
    wait (a slot, memory or a turn budget is full): nothing ran and nothing was recorded, and the queue tries again."""

    run_id: str
    waited: str | None = None


# True while `ws run` itself runs: its refusals and failures are final, so each is told once through the notify role.
# A queued run, and an eval's, leave it off: the queue says a stop only when it gives the message up, not at each retry.
_says_stops: contextvars.ContextVar[bool] = contextvars.ContextVar("ws_run_says_stops", default=False)


_told: list[str] = []  # jobs whose stop was already pushed in this process


def _alert_on_exit(name: str, code: int) -> None:
    """launchd has no OnFailure=, so a scheduled run (WS_ALERT_ON_EXIT=1 in its plist) that failed and was not already
    pushed as a stop sends the failure alert itself."""
    if (
        code and code != 143 and os.environ.get("WS_ALERT_ON_EXIT") == "1" and name not in _told
    ):  # 143: the scheduler stopped it
        jobwatch.alert_unit(f"ws.job.{name}", os.environ, status=lambda _u: ("exit-code", str(code)), job=name)


def _died(name: str, old: dict) -> None:
    """One record for a run whose holder stopped beating: it never finished, and nothing else would say so."""
    now = datetime.now(UTC)
    rec = runrecord.new(
        run_id=runrecord.new_run_id(now), job=name, started_at=runrecord.iso(now), ended_at=runrecord.iso(now),
        status="error", stop_reason="died", duration_ms=0, ws_version=__version__,
        error=f"the previous run of {name} (fence {old.get('fence', '?')}) stopped without finishing: its lock went stale",
    )  # fmt: skip
    _append(runrecord.runs_path(os.environ), rec)


def cmd_run(args: argparse.Namespace) -> int:
    token = _says_stops.set(True)
    name = getattr(args, "job", None)
    try:
        if args.dry_run or not isinstance(name, str) or not jobs.NAME.match(name):
            with _term_becomes_exit():
                return _run(args)
        with joblock.hold(name, os.environ, on_died=_died) as lock:
            if lock is None:  # a live run holds it: one tick, one run
                print(f"ws: {name} is already running; this start is skipped", file=sys.stderr)
                return 0
            code = 1  # what a run that raised counts as
            try:
                with _term_becomes_exit():
                    code = _run(args)
                return code
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                raise
            finally:
                _alert_on_exit(name, code)
                joblog.rotate(Path(os.path.abspath(paths.state_dir(os.environ))) / "logs" / f"{name}.log")
    finally:
        _says_stops.reset(token)


def run_job(
    args: argparse.Namespace, job: jobs.Job, fields: dict | None = None, hook=None, queued: Queued | None = None
) -> int:
    """`ws run` for a job that is already loaded (`ws eval` changes its prompt, folder and limits). `fields` is merged
    into the record when it is made; `hook(rec)` is called with the finished record just after it is written (and its
    reservation settled). `queued` is set by the queue's drain."""
    with _term_becomes_exit():
        return _run(args, job, fields, hook, queued)


def _bad_ledger(job: jobs.Job, e: WsError, log: Path, record: bool) -> int:
    return _bad_job(job.name, e.message, log, job, record)


def _run(
    args: argparse.Namespace,
    job: jobs.Job | None = None,
    fields: dict | None = None,
    hook=None,
    queued: Queued | None = None,
) -> int:
    env = os.environ
    home = paths.state_dir(env).absolute()
    log = runrecord.runs_path(env)
    record = not args.dry_run
    if job is None:
        try:
            job = jobs.load(args.job, env)
        except jobs.JobError as e:
            return _bad_job(args.job, str(e), log, record=record)
    try:
        adapter = adapters.get(job.runner)
        prof = load(profile_path(env)) or {}
        pol = policy.load()
    except (KeyError, ProfileError, policy.PolicyError) as e:
        return _bad_job(job.name, str(e.args[0] if isinstance(e, KeyError) else e), log, job, record)
    if job.purpose is not None and job.purpose not in pol.budget_purposes:
        known = ", ".join(sorted(pol.budget_purposes)) or "none"
        return _bad_job(
            job.name,
            f"job {job.name}: purpose {job.purpose!r} is not in [budget.purposes] of the policy (known: {known})",
            log, job, record,
        )  # fmt: skip
    parent = getattr(args, "parent", None)
    if parent:
        if not jobs.NAME.match(parent):
            return _bad_job(
                job.name, f"--parent {parent!r} is not a run id (letters, digits, - and _)", log, job, record
            )
        job = dataclasses.replace(job, parent_run=parent)
    started = datetime.now(UTC)  # the one clock reading of this run: its month, its lease, its deadline check
    run_id = queued.run_id if queued else runrecord.new_run_id(started)
    run_dir = home / "runs" / run_id
    # no cwd in the job: a fresh empty folder, outside ws's folders (the wall makes those read-only)
    cwd = Path(job.cwd).expanduser() if job.cwd else default_cwd(home, run_id)
    try:
        argv, extra = adapter.build_command(job, run_dir, _PLAN_LEASE if job.secrets else None)
    except ValueError as e:
        return _bad_job(job.name, f"job {job.name}: {e}", log, job, record)
    if job.cwd and not cwd.is_dir():
        return _bad_job(job.name, f"job {job.name}: cwd {str(cwd)!r} is not a folder", log, job, record)
    stdin = bool(getattr(adapter, "PROMPT_VIA_STDIN", False))
    if not stdin:
        argv = [*argv, "--", job.prompt]
    cap, unknown_run_usd, fallback_usd_per_mtok = _budget(prof)
    try:
        ledger = queue.ledger_from_profile(prof, home, log)
    except WsError as e:
        return _bad_ledger(job, e, log, record)
    lock_reason = _lock_reason(log)  # first: a lock held by another process would stall the read below as well

    def month_spend() -> queue.Spend:
        by = runrecord.month_spend_by_purpose(runrecord.read(log), started, unknown_run_usd, fallback_usd_per_mtok)
        return queue.Spend(sum(by.values()), {k: v for k, v in by.items() if k})

    verdict = policy.rule_of_two(pol, list(job.sources), job.name, shell=getattr(adapter, "HAS_SHELL", True))
    static = _refusal(prof, verdict, job, adapter)
    egress, why_not = _egress(pol, job)
    if static is None and why_not:
        static = ("egress", why_not)
    if static is None:
        walled = _wall_problem(pol, job, adapter, cwd)
        static = ("wall", walled) if walled else None
    req = queue.request_for(
        job, run_id, cap_usd=cap, unknown_run_usd=unknown_run_usd,
        purpose_cap_usd=pol.budget_purposes.get(job.purpose) if job.purpose else None, limits=ledger.limits,
    )  # fmt: skip
    rec = _base_record(job, run_id, started, pol, verdict)
    rec.update(fields or {})
    child_env = _child_env(extra)
    if args.dry_run:
        try:
            spend = queue.Spend() if lock_reason else month_spend()
        except OSError as e:
            print(f"ws: cannot read {log}: {e.strerror or type(e).__name__}", file=sys.stderr)
            return EXIT_FAILED
        verdict_now = None if lock_reason or static else ledger.admit(req, started, lambda: spend, dry=True)
        would = (
            lock_reason
            or (static[1] if static else None)
            or (verdict_now.reason if verdict_now and verdict_now.kind != "admit" else None)
        )
        plan = {
            "job": job.name, "runner": job.runner, "argv": argv, "prompt_via": "stdin" if stdin else "argv",
            "cwd": str(cwd or "<a private folder under the temp dir>"), "timeout_s": job.timeout_s, "env": sorted(child_env),
            "secrets": [{"name": s.name, "host": s.host} for s in job.secrets], "policy": rec["policy"],
            "month_spend_usd": round(spend.total, 6), "monthly_cap_usd": cap, "would_refuse": would,
            "purpose": job.purpose, "admission": verdict_now.kind if verdict_now else "not asked",
        }  # fmt: skip
        print(json.dumps(plan, indent=2))
        return EXIT_REFUSED if would else 0
    if lock_reason:
        return _refuse(rec, lock_reason, log, "refused:run_log_locked")
    if static:
        return _refuse(rec, static[1], log, f"refused:{static[0]}")
    try:
        decision = ledger.admit(req, started, month_spend)
    except queue.SpendUnreadable as e:
        print(f"ws: cannot read {log}: {e}", file=sys.stderr)
        return EXIT_FAILED
    if decision.kind == "wait" and queued is not None:
        queued.waited = decision.counter
        print(f"ws: {job.name} waits: {decision.reason}", file=sys.stderr)
        return EXIT_REFUSED
    if decision.kind != "admit":
        status = "expired" if decision.counter == "expired" else "refused"
        return _refuse(rec, decision.reason, log, decision.stop_reason, status)
    res = decision.reservation
    rec["reservation"] = res.as_record()
    if res.max_budget_usd is not None:
        job = dataclasses.replace(job, max_budget_usd=res.max_budget_usd)
    if job.checkpoint:
        extra = {**extra, "WS_CHECKPOINT": job.checkpoint}
        child_env = _child_env(extra)
    exe = _find_exe(argv[0], child_env)
    if exe is None:
        rec.update(
            status="error", error=f"{job.runner}: `{argv[0]}` is not on PATH"[:300], ended_at=rec["started_at"],
            stop_reason="not_found", duration_ms=0,
        )  # fmt: skip
        written = _append(log, rec)
        _settle(ledger, res, rec, written, (unknown_run_usd, fallback_usd_per_mtok))
        print(f"ws: {rec['error']}", file=sys.stderr)
        _announce_stop(rec)
        return EXIT_FAILED
    rec["exe_path"] = exe
    return _execute(
        job,
        adapter,
        pol,
        rec,
        run_dir,
        cwd,
        log,
        exe,
        stdin,
        hook,
        (ledger, res, unknown_run_usd, fallback_usd_per_mtok),
        egress,
    )


# receipt status for each record status (a timeout is an error of the run; a crash or Ctrl-C is an interruption)
_RECEIPT_STATUS = {"ok": "ok", "refused": "refused", "preempted": "preempted", "expired": "expired"}


def _settle(ledger: queue.Ledger, res: queue.Reservation, rec: dict, written: bool, costing: tuple) -> None:
    """Close the reservation from what the record says the run used (the same costing the cap check uses). Once, and
    after the record is written. A store that cannot be written now leaves the row to the next reaper, which finds the
    record by its run id and settles from it, so the spend is neither lost nor counted twice."""
    status = _RECEIPT_STATUS.get(rec["status"]) or (
        "interrupted" if rec.get("stop_reason") == "interrupted" else "error"
    )
    usd = runrecord.record_cost(rec, *costing)
    try:
        ledger.settle(
            res, status=status, usd=usd, tokens=rec.get("tokens"), stop_reason=rec.get("stop_reason"),
            now=datetime.now(UTC), record_written=written,
        )  # fmt: skip
    except (WsError, sqlqueue.StaleClaim) as e:
        print(f"ws: could not settle the reservation for {rec['run_id']}: {getattr(e, 'message', e)}", file=sys.stderr)


def _claude_wall(
    job, adapter, pol, run_dir: Path, cwd: Path, files: dict[str, str], walls: contextlib.ExitStack
) -> None:
    """For Claude Code with the sandbox on, make the run's settings the ones a `ws gate launch` would use for this folder:
    the project's locked paths and the home-folder denial added, a walled class's launch file merged in
    (`sandbox.claude_launch_text`), and the project's missing load paths made and let go of with `walls`
    (`sandbox.claude_loaded`). The file is the run's own copy in `run_dir/gate`; the network list is in it already. Settings
    with no sandbox block (`[gate] sandbox = false`) stay as they are, and so does any other runner."""
    if not hasattr(adapter, "for_project") or "sandbox" not in json.loads(files[adapter.FILE]):
        return
    from ws import sandbox

    env = _child_env({})
    cls = _job_class(pol, job)
    try:
        project = sandbox.project_folder(str(cwd), env)
        text = sandbox.claude_launch_text(
            pol,
            cls,
            project,
            env,
            None,
            files[adapter.FILE],
            cwd=str(cwd),
            launch_doc=sandbox.class_launch_doc(cls),
        )
        gate.write_files(run_dir / "gate", {adapter.FILE: text})
        walls.enter_context(sandbox.claude_loaded(paths.state_dir(os.environ).absolute(), project, job.runner))
    except (sandbox.SandboxError, WsError) as e:  # WsError: a launch file or a project the wall refuses
        raise _WallRefused(f"refusing to start {job.runner}: {e}") from e


class _WallRefused(Exception):
    """The wall could not be put round the runner (no bubblewrap, a link in the project, ...): the run is recorded as
    `refused:wall` and nothing was started."""


def _runner_wall(
    job, adapter, pol, cwd: Path, argv: list[str], child_env: dict, egress, walls: contextlib.ExitStack
) -> tuple[list[str], dict, tuple[int, ...]]:
    """(argv, env, descriptors to keep open) for a runner with no sandbox of its own (OpenCode) with the sandbox on: the
    same wall `ws gate launch` builds (`sandbox.walled`, held until `walls` is closed). The project is `cwd`; the runner's
    private XDG folders and TMPDIR are in the environment it gets, and the egress proxy is the one in the wall. Any other
    runner, or the sandbox off, comes back as it was."""
    if hasattr(adapter, "for_project") or not pol.gate.sandbox:
        return argv, child_env, ()
    from ws import sandbox

    lookup = {**os.environ, **child_env}  # where the real home is, and the generated folder the runner is told to read
    try:
        wall = walls.enter_context(
            sandbox.walled(
                argv,
                pol,
                paths.state_dir(os.environ).absolute(),
                job.runner,
                sandbox.project_folder(str(cwd), lookup),
                lookup,
                egress,
                protect=sandbox.protected(policy.policy_path().absolute(), job.runner, None, lookup),
                cls=_job_class(pol, job),
                cwd=str(cwd),
            )
        )
    except (sandbox.SandboxError, WsError) as e:  # WsError: `refused` from a launch file or the runner's project check
        raise _WallRefused(f"refusing to start {job.runner} without its wall: {e}") from e
    return wall.argv, {**child_env, sandbox.ENV_MARKER: "1", **wall.env}, wall.pass_fds


def _execute(
    job, adapter, pol, rec: dict, run_dir: Path, cwd: Path, log: Path, exe: str, stdin: bool, hook=None, admitted=None,
    egress=None,
) -> int:  # fmt: skip
    ledger, res, unknown_run_usd, fallback_usd_per_mtok = admitted
    t0 = time.monotonic()
    leases: list[broker.Lease] = []
    walls = contextlib.ExitStack()  # what the wall holds for the run, let go of in the finally below
    sock = wsecrets.socket_path(os.environ)
    signalled: BaseException | None = None
    try:
        run_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700)
        cwd = cwd if job.cwd else _make_default_cwd(cwd)  # a job's own cwd exists already
        rec["cwd"] = str(cwd)
        policy_file = policy.policy_path().absolute()
        files = adapter.generate(pol, selfcmd.argv(), run_dir / "gate", policy_file)
        if egress is not None and hasattr(adapter, "with_hosts"):  # the runner's own sandbox holds the class's list
            files = {**files, adapter.FILE: adapter.with_hosts(files[adapter.FILE], egress.domains())}
        gate.write_files(run_dir / "gate", files)
        _claude_wall(job, adapter, pol, run_dir, cwd, files, walls)
        for s in job.secrets:
            leases.append(_lease(s, sock, rec))
            rec["secrets_used"].append({"name": s.name, "host": s.host})
        argv, extra = adapter.build_command(job, run_dir, leases[0] if leases else None)
        if job.checkpoint:
            extra = {**extra, "WS_CHECKPOINT": job.checkpoint}
        child_env = _child_env(extra)
        argv[0] = exe
        prompt_file = None
        if stdin:
            prompt_file = run_dir / "prompt.txt"
            _write_private(prompt_file, job.prompt)
        else:
            argv += ["--", job.prompt]
        rec["runner_version"] = exe_version(exe, child_env) or None
        argv, child_env, pass_fds = _runner_wall(job, adapter, pol, cwd, argv, child_env, egress, walls)
        code, timed_out, truncated = _spawn(
            argv, child_env, cwd, job.timeout_s, prompt_file, run_dir, job.grace_s, pass_fds
        )
        rec.update(ended_at=runrecord.iso(datetime.now(UTC)), exit_code=code, output_truncated=truncated)
        out, err = _read_back(run_dir / "stdout.txt", leases), _read_back(run_dir / "stderr.txt", leases)
        part = adapter.parse_result(out, err, code)
        _merge(rec, part)
        for m in rec["models_used"]:
            m["model"] = m.get("model") or job.model
        if timed_out:
            rec.update(status="timeout", error=f"stopped after the {job.timeout_s:g} s timeout", stop_reason="timeout")
        elif code in (143, -signal.SIGTERM):
            rec.update(status="preempted", error=f"{job.runner} was stopped by SIGTERM", stop_reason="preempted")
        elif code != 0 or part.get("error"):
            last = (err.strip().splitlines() or [""])[-1]
            rec.update(
                status="error", stop_reason="runner_error",
                error=_short(part.get("error") or f"{job.runner} exited {code}: {last}", leases),
            )  # fmt: skip
        else:
            rec.update(status="ok", stop_reason="completed")
        with contextlib.suppress(Exception):
            rec["transcript_path"] = adapter.transcript_path(rec["session_id"], str(cwd), child_env)
    except _WallRefused as e:
        rec.update(status="refused", stop_reason="refused:wall", error=_short(str(e), leases, 300))
    except broker.BrokerRefused as e:
        rec.update(
            status="refused", stop_reason="refused:secret", error=_short(f"broker refused the secret: {e}", leases)
        )
    except broker.BrokerUnavailable as e:
        tried = f" (ws already {rec['recovered'][-1]['action']}, once)" if rec["recovered"] else ""
        rec.update(
            status="error", stop_reason="broker_unavailable", error=_short(f"{e}{tried}; {broker._hint(e)}", leases, 300)
        )  # fmt: skip

    except Exception as e:  # noqa: BLE001 (whatever went wrong, the run still gets its record)
        rec.update(status="error", stop_reason="error", error=_short(f"{type(e).__name__}: {e}", leases))
    except BaseException as e:  # Ctrl-C or SIGTERM: the finally below records it, then it goes on up
        signalled = e
        raise
    finally:
        try:
            walls.close()
        except Exception as e:  # noqa: BLE001 - the leases, the record and the settle below never wait on the wall
            print(f"ws: could not release the wall ({type(e).__name__})", file=sys.stderr)
        for lease in leases:
            with contextlib.suppress(broker.BrokerError):
                broker.release(lease, socket=sock)
        rec["ended_at"] = rec["ended_at"] or runrecord.iso(datetime.now(UTC))
        rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if rec["status"] is None:  # Ctrl-C, SIGTERM or another exit that is not an Exception
            sigterm = isinstance(signalled, SystemExit) and signalled.code == 128 + signal.SIGTERM
            rec.update(
                status="preempted" if sigterm else "error", stop_reason="preempted" if sigterm else "interrupted",
                error="interrupted before the runner finished",
            )  # fmt: skip
            print(f"ws: interrupted; recorded as {rec['run_id']}", file=sys.stderr)
        written = _append(log, rec)
        _settle(ledger, res, rec, written, (unknown_run_usd, fallback_usd_per_mtok))
        _announce_stop(rec)
        if hook is not None:  # after the write: a signal or a crash in the hook cannot lose the record
            with contextlib.suppress(Exception):
                hook(rec)
    _report(rec, log)
    code = {"ok": 0, "refused": EXIT_REFUSED}.get(rec["status"], EXIT_FAILED)
    return code or (0 if written else EXIT_FAILED)


def _report(rec: dict, log: Path) -> None:
    t = rec["tokens"]
    bits = [f"{t['input']} in / {t['output']} out tokens" if t["input"] is not None else "tokens unknown"]
    bits.append(f"US${rec['cost_usd']:.4f}" if rec["cost_usd"] is not None else "cost unknown")
    bits.append(f"{rec['duration_ms'] / 1000:.1f} s")
    print(f"ws: run {rec['run_id']} {rec['status']}: {rec['job']} on {rec['model_requested']}, {', '.join(bits)}")
    if rec["error"]:
        print(f"ws: {rec['error']}", file=sys.stderr)
    if rec["output_truncated"]:
        print(f"ws: the runner's output was cut at {OUTPUT_CAP // (1024 * 1024)} MiB", file=sys.stderr)
    print(f"ws: record in {log}" + (f"; transcript {rec['transcript_path']}" if rec["transcript_path"] else ""))
    print(f"ws: rate: ws rate {rec['run_id']} up | down")


def cmd_runs(args: argparse.Namespace) -> int:
    log = runrecord.runs_path()
    try:
        recs = runrecord.read(log)[-args.last :]
    except OSError as e:
        raise WsError("io", f"cannot read {log}: {e.strerror or type(e).__name__}", exit_code=EXIT_FAILED) from e
    pending = runrecord.fallback_count(log)
    if pending:
        print(
            f"ws: {pending} record(s) are in {runrecord.fallback_path(log)}: the run log could not be written when they "
            "finished (another process held its lock). They count towards the cap; look into what held the lock.",
            file=sys.stderr,
        )
    if args.json:
        output.result(recs)
        return 0
    if not recs:
        print("ws: no runs yet")
        return 0
    for r in recs:
        t = r.get("tokens") or {}
        cost = r.get("cost_usd")
        print(
            f"{r.get('run_id')}  {r.get('status')!s:<8}  {r.get('job')}  {r.get('runner')}  {r.get('model_requested')}"
            f"  {t.get('input')}/{t.get('output')} tok  {'-' if cost is None else f'US${cost:.4f}'}"
        )
    return 0


def _positive(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def register(sub) -> None:
    r = sub.add_parser(
        "run",
        help="run a headless job (a file in the config folder's jobs/) and record what it did",
        description="Run a headless job. The monthly cap is checked before the run, not enforced during it.",
    )
    r.add_argument("job", help="the job name: jobs/<name>.toml in the config folder")
    r.add_argument("--dry-run", action="store_true", help="show what would start, and whether it would be refused")
    r.add_argument(
        "--parent",
        metavar="RUN_ID",
        help="reserve only from what this run in progress has left (on top of the monthly cap)",
    )
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("runs", help="show the last records of `ws run`")
    s.add_argument("--last", type=_positive, default=10, metavar="N", help="how many (default 10)")
    s.set_defaults(func=cmd_runs)
