"""`ws run JOB`: every headless job goes through here and leaves one complete record in `runs.jsonl`.

    ws run JOB [--dry-run]         run `$WS_HOME/jobs/JOB.toml`; --dry-run shows what would start and starts nothing
    ws runs [--last N] [--json]    the last N records (default 10)

The steps: load the job, profile and policy; refuse (exit 3, and still write a record with status "refused") when there
is no monthly cap, this month's spend is at the cap, the Rule of Two says no, or the runner cannot price the model and
the job does not say `allow_unpriced = true`; write the runner's gate config into a per-run folder
`$WS_HOME/runs/<run_id>/gate` (the gate denies the agent writes to `runs/` and `logs/`, so those hold only
what ws wrote); lease each secret from the broker (the runner only ever holds a placeholder key and the
lease's base URL); start the runner with a scrubbed environment, the prompt on stdin and the job's timeout; keep its
output in files in the run folder (capped); read that into the record; release the leases whatever happened, including a
Ctrl-C or a SIGTERM to `ws run`.

The monthly cap is checked before each run. It is not enforced during one: a run that has started can spend what its
own timeout and turns allow, so keep `timeout_s` and `max_turns` tight. A run whose cost the runner did not report
counts at the person's own fallback ([roles.budget] fallback_usd_per_mtok, else unknown_run_usd), so spend is
never undercounted. ws ships no prices.

Exit: 0 ok, 1 the runner failed or timed out, 2 the job could not be read (a record is written when the job name is
safe), 3 refused.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__, adapters, broker, gate, jobs, policy, runrecord
from ws import secrets as wsecrets
from ws.profile import ProfileError, load, profile_path, ws_home

# The only variables the runner inherits from ws. Anything else it needs is set by the adapter or the job file.
PASS_ENV = ("PATH", "HOME", "LANG", "TZ", "TERM")
EXIT_FAILED, EXIT_BAD_JOB, EXIT_REFUSED = 1, 2, 3
OUTPUT_CAP = 32 * 1024 * 1024  # bytes kept of each of stdout and stderr; the rest is read and dropped
TERM_GRACE_S = 3.0  # how long a runner has to stop after SIGTERM before its whole group gets SIGKILL
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


def _rule_label(v: policy.Verdict) -> str:
    return "refused" if not v.ok else f"exception:{v.exception}" if v.exception else "ok"


def _redact(text: str, leases: list[broker.Lease]) -> str:
    """Take any lease URL, or the token in it, out of text before it is stored or shown."""
    for lease in leases:
        text = text.replace(lease.base_url, "<lease>").replace(lease.base_url.rsplit("/", 1)[-1], "<lease>")
    return text


def _short(text: str, leases: list[broker.Lease], n: int = 200) -> str:
    return _redact(" ".join(str(text).split()), leases)[:n]


def _child_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update(extra)
    env["WS_HOME"] = str(home)  # last: where the gate hook finds the policy
    return env


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


def _kill(p: subprocess.Popen) -> None:
    """Stop the runner and everything it started: SIGTERM, a short grace, then SIGKILL to the whole group. The SIGKILL
    is sent even when the runner itself has gone, because a child that ignores SIGTERM outlives it."""
    _signal_group(p, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        p.wait(timeout=TERM_GRACE_S)
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
    argv: list[str], env: dict[str, str], cwd: Path, timeout_s: float, stdin_path: Path | None, out_dir: Path
) -> tuple[int | None, bool, bool]:
    """Run the runner, its prompt from `stdin_path`, its output going to out_dir/stdout.txt and stderr.txt.
    Returns (exit code or None if killed, timed out, output truncated). A runner that overruns, or a `ws run` that is
    interrupted, kills the runner's whole process group before anything else happens."""
    stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL  # noqa: SIM115
    try:
        p = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )  # fmt: skip
    finally:
        if stdin_path:
            stdin.close()
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
            _kill(p)
        _signal_group(p, signal.SIGKILL)  # whatever the runner left behind holds the pipes open
    except BaseException:
        _kill(p)
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
    )


def _refusal(prof: dict, verdict: policy.Verdict, spend: float, job: jobs.Job, adapter) -> str | None:
    cap = _budget(prof)[0]
    if cap is None:
        return "no monthly budget cap (WS103): set monthly_cap_usd in [roles.budget] of the profile"
    if spend >= cap:
        return f"this month's spend, US${spend:.4f}, is at the monthly cap of US${cap:g}"
    if not verdict.ok:
        return verdict.reason
    if not job.allow_unpriced and not getattr(adapter, "priced", lambda _job: True)(job):
        return (
            f"{job.runner} cannot say what {job.model} costs, so the cap could not be kept; set allow_unpriced = true "
            "in the job to run it anyway (it then counts at your fallback price)"
        )
    return None


def _append(log: Path, rec: dict) -> bool:
    """Write the record. False (after saying why) when the log cannot be written."""
    try:
        runrecord.append(log, rec)
    except OSError as e:
        print(f"ws: could not write the record to {log}: {e.strerror or type(e).__name__}", file=sys.stderr)
        return False
    return True


def _refuse(rec: dict, reason: str, path: Path) -> int:
    rec.update(status="refused", error=reason[:300], ended_at=rec["started_at"], duration_ms=0)
    _append(path, rec)
    print(f"ws: refused: {reason}", file=sys.stderr)
    print(f"ws: refusal recorded as {rec['run_id']}", file=sys.stderr)
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
    )
    if _append(log, rec):
        print(f"ws: recorded as {rec['run_id']}", file=sys.stderr)
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


def cmd_run(args: argparse.Namespace) -> int:
    with _term_becomes_exit():
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    env = os.environ
    home = ws_home(env).absolute()
    log = runrecord.runs_path(env)
    record = not args.dry_run
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
    started = datetime.now(UTC)
    run_id = runrecord.new_run_id(started)
    run_dir = home / "runs" / run_id
    # no cwd in the job: a fresh empty folder. Not under runs/, which the agent may not write to.
    cwd = Path(job.cwd).expanduser() if job.cwd else home / "work" / run_id
    try:
        argv, extra = adapter.build_command(job, run_dir, _PLAN_LEASE if job.secrets else None)
    except ValueError as e:
        return _bad_job(job.name, f"job {job.name}: {e}", log, job, record)
    if job.cwd and not cwd.is_dir():
        return _bad_job(job.name, f"job {job.name}: cwd {str(cwd)!r} is not a folder", log, job, record)
    stdin = bool(getattr(adapter, "PROMPT_VIA_STDIN", False))
    if not stdin:
        argv = [*argv, "--", job.prompt]
    _, unknown_run_usd, fallback_usd_per_mtok = _budget(prof)
    try:
        spend = runrecord.month_spend(runrecord.read(log), started, unknown_run_usd, fallback_usd_per_mtok)
    except OSError as e:
        print(f"ws: cannot read {log}: {e.strerror or type(e).__name__}", file=sys.stderr)
        return EXIT_FAILED
    verdict = policy.rule_of_two(pol, list(job.sources), job.name, shell=getattr(adapter, "HAS_SHELL", True))
    reason = _refusal(prof, verdict, spend, job, adapter)
    rec = _base_record(job, run_id, started, pol, verdict)
    child_env = _child_env(home, extra)
    if args.dry_run:
        plan = {
            "job": job.name, "runner": job.runner, "argv": argv, "prompt_via": "stdin" if stdin else "argv",
            "cwd": str(cwd), "timeout_s": job.timeout_s, "env": sorted(child_env),
            "secrets": [{"name": s.name, "host": s.host} for s in job.secrets], "policy": rec["policy"],
            "month_spend_usd": round(spend, 6), "monthly_cap_usd": _budget(prof)[0], "would_refuse": reason,
        }  # fmt: skip
        print(json.dumps(plan, indent=2))
        return EXIT_REFUSED if reason else 0
    if reason:
        return _refuse(rec, reason, log)
    exe = _find_exe(argv[0], child_env)
    if exe is None:
        rec.update(status="error", error=f"{job.runner}: `{argv[0]}` is not on PATH"[:300], ended_at=rec["started_at"])
        rec["duration_ms"] = 0
        _append(log, rec)
        print(f"ws: {rec['error']}", file=sys.stderr)
        return EXIT_FAILED
    rec["exe_path"] = exe
    return _execute(job, adapter, pol, rec, run_dir, cwd, log, exe, stdin)


def _execute(job, adapter, pol, rec: dict, run_dir: Path, cwd: Path, log: Path, exe: str, stdin: bool) -> int:
    t0 = time.monotonic()
    leases: list[broker.Lease] = []
    sock = wsecrets.socket_path(os.environ)
    try:
        run_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700)
        cwd.mkdir(parents=True, exist_ok=True)
        rec["cwd"] = str(cwd)
        policy_file = policy.policy_path().absolute()
        files = adapter.generate(pol, [sys.executable, "-m", "ws"], run_dir / "gate", policy_file)
        gate.write_files(run_dir / "gate", files)
        for s in job.secrets:
            leases.append(broker.request(s.name, s.purpose, s.host, socket=sock))
            rec["secrets_used"].append({"name": s.name, "host": s.host})
        argv, extra = adapter.build_command(job, run_dir, leases[0] if leases else None)
        child_env = _child_env(ws_home().absolute(), extra)
        argv[0] = exe
        prompt_file = None
        if stdin:
            prompt_file = run_dir / "prompt.txt"
            _write_private(prompt_file, job.prompt)
        else:
            argv += ["--", job.prompt]
        rec["runner_version"] = exe_version(exe, child_env) or None
        code, timed_out, truncated = _spawn(argv, child_env, cwd, job.timeout_s, prompt_file, run_dir)
        rec.update(ended_at=runrecord.iso(datetime.now(UTC)), exit_code=code, output_truncated=truncated)
        out, err = _read_back(run_dir / "stdout.txt", leases), _read_back(run_dir / "stderr.txt", leases)
        part = adapter.parse_result(out, err, code)
        _merge(rec, part)
        for m in rec["models_used"]:
            m["model"] = m.get("model") or job.model
        if timed_out:
            rec.update(status="timeout", error=f"stopped after the {job.timeout_s:g} s timeout")
        elif code != 0 or part.get("error"):
            last = (err.strip().splitlines() or [""])[-1]
            rec.update(status="error", error=_short(part.get("error") or f"{job.runner} exited {code}: {last}", leases))
        else:
            rec["status"] = "ok"
        with contextlib.suppress(Exception):
            rec["transcript_path"] = adapter.transcript_path(rec["session_id"], str(cwd), child_env)
    except broker.BrokerRefused as e:
        rec.update(status="refused", error=_short(f"broker refused the secret: {e}", leases))
    except broker.BrokerUnavailable as e:
        rec.update(status="error", error=_short(str(e), leases))
    except Exception as e:  # noqa: BLE001 (whatever went wrong, the run still gets its record)
        rec.update(status="error", error=_short(f"{type(e).__name__}: {e}", leases))
    finally:
        for lease in leases:
            with contextlib.suppress(broker.BrokerError):
                broker.release(lease, socket=sock)
        rec["ended_at"] = rec["ended_at"] or runrecord.iso(datetime.now(UTC))
        rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if rec["status"] is None:  # Ctrl-C, SIGTERM or another exit that is not an Exception
            rec.update(status="error", error="interrupted before the runner finished")
            print(f"ws: interrupted; recorded as {rec['run_id']}", file=sys.stderr)
        written = _append(log, rec)
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
    print(f"rate: ws rate {rec['run_id']} up | down")


def cmd_runs(args: argparse.Namespace) -> int:
    log = runrecord.runs_path()
    try:
        recs = runrecord.read(log)[-args.last :]
    except OSError as e:
        print(f"ws: cannot read {log}: {e.strerror or type(e).__name__}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps(recs, indent=2))
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
        help="run a headless job (a file in $WS_HOME/jobs) and record what it did",
        description="Run a headless job. The monthly cap is checked before the run, not enforced during it.",
    )
    r.add_argument("job", help="the job name: $WS_HOME/jobs/<name>.toml")
    r.add_argument("--dry-run", action="store_true", help="show what would start, and whether it would be refused")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("runs", help="show the last records of `ws run`")
    s.add_argument("--last", type=_positive, default=10, metavar="N", help="how many (default 10)")
    s.add_argument("--json", action="store_true", help="the records as a JSON array")
    s.set_defaults(func=cmd_runs)
