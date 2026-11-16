"""The commands the role adapters bring: `ws notify`, `ws backup` and `ws schedule`.

    ws notify test|send|status      a test push, a push or digest, today's count against the caps
    ws notify send --from-file F --click URL   the first line of a summary, with a link a tap opens
    ws notify alert                 hook entry: "<folder> needs you" or "<folder> finished" to your phone
    ws backup init|run|verify       make the restic repository, back up, prove a file restores
    ws schedule list|apply|remove   timers for the job files that have a `schedule`
    ws schedule script NAME         run a `command = "script"` job's reviewed script (what its timer runs): refused
                                    unless the script is yours, closed to other writers and hashes to script_sha256;
                                    the bytes that were hashed are what runs (a private copy under ws's state folder),
                                    in the script's folder. Only the script is hashed: a file it sources or runs by a
                                    path of its own is outside the review, so a script should call only what it names
                                    by absolute path and trusts.

Each reads its adapter's settings from `[roles.<role>]` of the profile. Messages never hold a secret.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ws import install, jobs, osinfo, output, paths
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path
from ws.roles import _schedule, ntfy, restic


def _role(name: str, adapter: str) -> dict:
    """[roles.<name>] from the profile, if it names this adapter. Raises ValueError (safe to print)."""
    try:
        roles = (load(profile_path()) or {}).get("roles")
    except ProfileError as e:
        raise ValueError(str(e)) from e
    cfg = roles.get(name) if isinstance(roles, dict) else None
    if not isinstance(cfg, dict) or cfg.get("adapter") != adapter:
        raise ValueError(f'no [roles.{name}] with adapter = "{adapter}" in the profile (run `ws doctor`)')
    return cfg


# ---- ws notify -------------------------------------------------------------------------------------------------------


def _notify(fn) -> int:
    try:
        print(f"ws: {fn(_role('notify', 'ntfy'))}")
    except (ValueError, ntfy.NotifyError) as e:
        raise WsError("failed", str(e)) from e
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    return _notify(lambda cfg: ntfy.send_test(cfg, os.environ))


LINE_MAX = 300  # the most of a summary file's first line that goes in a push
LINES_READ = 200  # how many lines are looked at for one with text


def first_line(path: str, fresh_min: float | None = None) -> str | None:
    """The first line with text in `path`, trimmed to LINE_MAX characters. None when `fresh_min` is given and the file
    is older than that many minutes (a summary from an earlier run is not news). Raises ValueError (safe to print)."""
    import stat
    import time
    from pathlib import Path

    f = Path(path).expanduser()
    try:
        st = f.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{f} is not a file")
        if fresh_min is not None and time.time() - st.st_mtime > fresh_min * 60:
            return None
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(LINES_READ):
                line = fh.readline(LINE_MAX * 4)  # bounded: a file with no line break can't fill memory
                if not line:
                    break
                if line.strip():
                    return " ".join(line.split())[:LINE_MAX]
    except OSError as e:
        raise ValueError(f"{f} cannot be read ({e.strerror or type(e).__name__})") from e
    raise ValueError(f"{f} has no line with text in it")


def cmd_notify_send(args: argparse.Namespace) -> int:
    kind = "digest" if args.digest else "push"
    if (args.message is None) == (args.from_file is None):
        raise WsError("usage", "give a message or --from-file PATH, not both", 'e.g. `ws notify send "done"`', 2)
    if args.fresh is not None and args.from_file is None:
        raise WsError("usage", "--fresh goes with --from-file", "", 2)
    if args.fresh is not None and not (0 < args.fresh < float("inf")):
        raise WsError("usage", "--fresh takes a number of minutes above 0", "", 2)
    message = args.message
    if args.from_file is not None:
        try:
            message = first_line(args.from_file, args.fresh)
        except ValueError as e:
            raise WsError("failed", str(e), "the job that writes the summary may not have run") from e
        if message is None:
            print(f"ws: not sent: {args.from_file} is older than {args.fresh:g} minutes (0 sent)")
            return 0
    return _notify(lambda cfg: ntfy.send(cfg, os.environ, message, args.title, kind, click=args.click))


def cmd_notify_status(args: argparse.Namespace) -> int:
    try:
        cfg, env = _role("notify", "ntfy"), os.environ
        cap = ntfy.caps(env)
        counts = ntfy.read_counts(env, ntfy.today(ntfy.zone(cfg, env)))
    except (ValueError, ntfy.NotifyError) as e:
        raise WsError("failed", str(e)) from e
    if args.json:
        output.result(
            {
                "push": counts["push"],
                "push_cap": cap["push"],
                "digest": counts["digest"],
                "digest_cap": cap["digest"],
                "alert": counts["alert"],
                "alert_cap": cap["alert"],
            }
        )
        return 0
    print(
        f"ws: today {counts['push']} of {cap['push']} pushes, {counts['digest']} of {cap['digest']} digest,"
        f" {counts['alert']} of {cap['alert']} alerts"
    )
    return 0


MAX_PAYLOAD = 1_000_000


def cmd_notify_alert(args: argparse.Namespace) -> int:
    """Hook entry. Pushes the alert the runner's hook JSON on stdin asks for. Advice, not a gate: always exits 0 and
    says on stderr why nothing went when something is wrong. Any failure counts, including a payload nested deep enough
    to hit the recursion limit, a missing stdin and a broker that is down."""
    try:
        # the profile says where the alert goes: read it from the folder `ws install` pinned, never from one the
        # session's environment names (the hook runs outside the sandbox)
        with paths.pinned_by(args.pin):
            roles = (load(profile_path()) or {}).get("roles")
            cfg = roles.get("notify") if isinstance(roles, dict) else None
            if not isinstance(cfg, dict) or cfg.get("adapter") != ntfy.NAME:
                return 0  # no phone alerts set up: nothing to do
            payload = None if sys.stdin.isatty() else json.loads(sys.stdin.read(MAX_PAYLOAD) or "null")
            line = ntfy.alert(cfg, paths.merged_environ(), payload, args.runner)
        if line:
            print(f"ws: {line}", file=sys.stderr)
    except WsError as e:
        _warn(f"ws: alert not sent ({e.message}; {e.hint})")
    except (ProfileError, ntfy.NotifyError, ValueError, OSError) as e:  # ValueError covers a payload that isn't JSON
        _warn(f"ws: alert not sent: {e}")
    except Exception as e:  # noqa: BLE001  a hook must never fail its session; the type is enough to go on
        _warn(f"ws: alert not sent ({type(e).__name__})")
    return 0


def _warn(text: str) -> None:
    """A line on stderr; a closed stderr is not worth failing a hook for."""
    with contextlib.suppress(Exception):
        print(text, file=sys.stderr)


# ---- ws backup -------------------------------------------------------------------------------------------------------


def _backup(fn) -> int:
    try:
        env = os.environ
        cfg = restic.role_config(env)
        if cfg.get("adapter") != restic.NAME:
            raise restic.BackupError('[roles.backup] needs adapter = "restic"')
        result = fn(cfg, env)
    except (restic.BackupError, ValueError) as e:
        raise WsError("failed", str(e)) from e
    if isinstance(result, restic.Verified):
        if not result.ok:
            raise WsError("failed", f"NOT verified: {result.detail}", "run `ws backup run`, then verify again")
        print(f"ws: verified: {result.detail}")
        return 0
    print(f"ws: {result}")
    return 0


def cmd_backup_init(args: argparse.Namespace) -> int:
    return _backup(restic.init)


def cmd_backup_run(args: argparse.Namespace) -> int:
    return _backup(restic.backup)


def cmd_backup_verify(args: argparse.Namespace) -> int:
    return _backup(restic.verify)


# ---- ws schedule -----------------------------------------------------------------------------------------------------


def _scheduler(osi: osinfo.OSInfo):
    """The scheduler module for this machine: the profile's choice, else the OS's own."""
    from ws import roles

    try:
        roles_cfg = (load(profile_path()) or {}).get("roles")
    except ProfileError as e:
        raise ValueError(str(e)) from e
    cfg = roles_cfg.get("scheduler") if isinstance(roles_cfg, dict) else None
    name = cfg.get("adapter") if isinstance(cfg, dict) and isinstance(cfg.get("adapter"), str) else osi.scheduler
    if name not in roles.adapters("scheduler"):
        raise ValueError(f"this machine has no scheduler ws can use ({name}); systemd and launchd are supported")
    if name != osi.scheduler:
        raise ValueError(f"{name} is set but this machine runs {osi.scheduler}")
    return roles.get("scheduler", name)


def cmd_schedule_list(args: argparse.Namespace) -> int:
    try:
        rows = [
            {"job": j.name, "when": str(j.when), "runs": "ws " + " ".join(j.argv)}
            for j in _schedule.scheduled(os.environ)
        ]
    except ValueError as e:
        raise WsError("failed", str(e)) from e
    if args.json:
        output.result(rows)
    else:
        for r in rows:
            print(f"{r['job']:<20} {r['when']:<18} {r['runs']}")
        if not rows:
            print('ws: no job file has a schedule (add schedule = "daily 03:30" to one in the jobs folder)')
    return 0


def _drop(sched, env, names: list[str]) -> tuple[int, list[str]]:
    """Switch these jobs' timers off, delete the unit files ws wrote for them and take them out of the manifest, so
    uninstall does not look for them. Returns (files deleted, problems)."""
    problems = sched.deactivate(names, env)
    gone = []
    for name in names:
        for path in sched.files_for(env, name):
            if path.is_file() and not path.is_symlink():
                path.unlink()
                gone.append(path)
    install.forget(gone, env)
    return len(gone), problems


def cmd_schedule_apply(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        sched = _scheduler(osinfo.detect())
        steps = sched.steps({}, install.answers_from_profile(env), env)
        result = install.apply(steps, env, dry_run=args.dry_run)
        stale = sched.orphans(env)  # a job whose schedule was removed: its timer must not stay on
        problems: list[str] = []
        if not args.dry_run:
            problems += _drop(sched, env, stale)[1]
            problems += sched.activate(env)
    except (ValueError, install.InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    for p in problems:
        print(f"ws: {p}", file=sys.stderr)
    tail = f"; {'would stop' if args.dry_run else 'stopped'} {', '.join(stale)} (no schedule now)" if stale else ""
    print(f"ws: {'dry run: ' if args.dry_run else ''}{len(steps)} unit file(s), {result.summary()}{tail}")
    return 1 if problems else 0


SCRIPT_COPY_MAX_AGE_S = 2 * 86400  # a copy this old was left by a run that was killed (a clean one removes its own)


def _sweep(folder: Path, before: float) -> None:
    """Remove the `run-*` copies older than `before` that a killed run left in `folder`. Never raises."""
    import shutil

    with contextlib.suppress(OSError):
        for d in folder.glob("run-*"):
            with contextlib.suppress(OSError):
                if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < before:
                    shutil.rmtree(d, ignore_errors=True)


SCRIPT_EXIT_TIMEOUT = 124  # as timeout(1): the script ran past the job's timeout_s and was killed


def checked_script(name: str, env) -> tuple[Path, bytes, float | None]:
    """(the script's path, the bytes that hash to the job's script_sha256, the job's timeout_s) of the script job `name`.
    Raises WsError (refused, exit 3) naming what is wrong: no such job, not a script job, a script someone else could
    have written, or one that no longer hashes to what was reviewed."""

    def refuse(why: str, hint: str = "") -> WsError:
        return WsError("refused", why, hint, 3)

    try:
        d = jobs.command_job(name, env)
        if d.get("command") != "script":
            raise refuse(f"job {name} is not a script job (command = {d.get('command')!r})")
        path, sha = jobs.script_of(d, f"job {name}", env)
        jobs.check_private(path.parent)
        jobs.check_private(path)
        data = path.read_bytes()
    except FileNotFoundError as e:
        raise refuse(f"no job {name!r}: expected {e}") from e
    except jobs.JobError as e:
        raise refuse(str(e)) from e
    except OSError as e:
        raise refuse(f"job {name}: the script cannot be read ({e.strerror or type(e).__name__})") from e
    if hashlib.sha256(data).hexdigest() != sha:
        raise refuse(
            f"job {name}: {path} no longer hashes to script_sha256, so nobody reviewed what it is now",
            "review it, then put its new `sha256sum` in the job file (and `ws job add --replace` a pinned job)",
        )
    timeout = d.get("timeout_s")
    ok = isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0
    return path, data, float(timeout) if ok else None


def cmd_schedule_script(args: argparse.Namespace) -> int:
    env = os.environ
    from ws import approvals

    if env.get(approvals.ENV_MARKER):  # a timer's command: it runs outside any session's wall
        raise WsError(
            "refused", f"ws schedule script is not for a gated session ({approvals.ENV_MARKER} is set here)",
            "run it yourself, in your own terminal, or let its timer run it", 3,
        )  # fmt: skip
    path, data, timeout = checked_script(args.job, env)
    private = paths.state_dir(env) / "scripts"  # ws's own state folder, not a temp folder others share
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.chmod(0o700)
    _sweep(private, time.time() - SCRIPT_COPY_MAX_AGE_S)
    with tempfile.TemporaryDirectory(prefix="run-", dir=private) as tmp:
        copy = Path(tmp) / path.name  # the reviewed bytes, where nobody else can swap them before they run
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            code = subprocess.run(
                [str(copy)], cwd=path.parent, env={**env, "WS_SCRIPT_PATH": str(path)}, stdin=subprocess.DEVNULL,
                timeout=timeout, check=False,
            ).returncode  # fmt: skip
        except subprocess.TimeoutExpired:
            print(f"ws: {args.job}: the script ran past its {timeout:g} s timeout and was stopped", file=sys.stderr)
            code = SCRIPT_EXIT_TIMEOUT
        except OSError as e:
            raise WsError("failed", f"job {args.job}: cannot start {path}: {e.strerror or type(e).__name__}",
                          "give it a #! line; a state folder mounted noexec also stops it", 1) from e  # fmt: skip
    code = 128 - code if code < 0 else code  # killed by signal N: 128+N, as a shell says it
    if code and env.get("WS_ALERT_ON_EXIT") == "1":  # launchd has no OnFailure=: the failure alert is sent from here
        from ws import jobwatch

        with contextlib.suppress(Exception):
            jobwatch.alert_unit(f"ws.job.{args.job}", env, status=lambda _u: ("exit-code", str(code)), job=args.job)
    return code


def cmd_schedule_remove(args: argparse.Namespace) -> int:
    """Stop a job's timer and delete the unit files ws wrote for it (the job file stays)."""
    env = os.environ
    try:
        if not jobs.NAME.match(args.job):
            raise ValueError("a job name is letters, digits, - and _")
        gone, problems = _drop(_scheduler(osinfo.detect()), env, [args.job])
    except (ValueError, install.InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    for p in problems:
        print(f"ws: {p}", file=sys.stderr)
    print(f"ws: removed {gone} unit file(s) for {args.job}")
    return 0


def register(sub) -> None:
    n = sub.add_parser(
        "notify", help="push to your phone through ntfy: 1 digest and 3 pushes a day, plus session alerts"
    )
    ns = n.add_subparsers(dest="notify_command", required=True)
    p = ns.add_parser("test", help="send a test push (it counts against the daily cap)")
    p.set_defaults(func=cmd_notify_test)
    p = ns.add_parser("send", help="send a push, or a digest with --digest, if today's cap allows")
    p.add_argument("message", nargs="?", help="the text (or --from-file)")
    p.add_argument("--from-file", metavar="PATH", help="send the first line with text in this file, such as a summary")
    p.add_argument("--fresh", type=float, metavar="MINUTES", help="with --from-file: send only if the file is newer")
    p.add_argument("--click", metavar="URL", help="the link the push opens when tapped (http or https)")
    p.add_argument("--title", default="ws")
    p.add_argument("--digest", action="store_true", help="the one daily digest, not a push")
    p.set_defaults(func=cmd_notify_send)
    p = ns.add_parser("status", help="today's count against the caps")
    p.set_defaults(func=cmd_notify_status)
    p = ns.add_parser("alert", help='hook entry: push "<folder> needs you" or "<folder> finished" (hook JSON on stdin)')
    p.add_argument("--runner", default="unknown", help="the runner whose hook is calling, for example claude-code")
    paths.add_pin_option(p)
    p.set_defaults(func=cmd_notify_alert)

    b = sub.add_parser("backup", help="back up memory, notes and profile with restic, and prove a file restores")
    bs = b.add_subparsers(dest="backup_command", required=True)
    for name, fn, text in (
        ("init", cmd_backup_init, "make the restic repository"),
        ("run", cmd_backup_run, "back up now (the backup job's timer runs this)"),
        ("verify", cmd_backup_verify, "restore one random file to a temporary folder and compare its hash"),
    ):
        bs.add_parser(name, help=text).set_defaults(func=fn)

    s = sub.add_parser("schedule", help="timers (systemd or launchd) for the job files that have a schedule")
    ss = s.add_subparsers(dest="schedule_command", required=True)
    p = ss.add_parser("list", help="the scheduled jobs")
    p.set_defaults(func=cmd_schedule_list)
    p = ss.add_parser("apply", help="write the unit files and switch the timers on; run again and it reports 0 changes")
    p.add_argument("--dry-run", action="store_true", help="show what would change; write and run nothing")
    p.set_defaults(func=cmd_schedule_apply)
    p = ss.add_parser("remove", help="stop one job's timer and delete its unit files")
    p.add_argument("job")
    p.set_defaults(func=cmd_schedule_remove)
    p = ss.add_parser(
        "script", help="run a script job's reviewed script now (what its timer runs); its own output and exit code"
    )
    p.add_argument("job", help='the job name: jobs/<name>.toml with command = "script"')
    p.set_defaults(func=cmd_schedule_script, raw=True)
