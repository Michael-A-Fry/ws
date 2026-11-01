"""The commands the role adapters bring: `ws notify`, `ws backup` and `ws schedule`.

    ws notify test|send|status      a test push, a push or digest, today's count against the caps
    ws notify alert                 hook entry: "<folder> needs you" or "<folder> finished" to your phone
    ws backup init|run|verify       make the restic repository, back up, prove a file restores
    ws schedule list|apply|remove   timers for the job files that have a `schedule`

Each reads its adapter's settings from `[roles.<role>]` of the profile. Messages never hold a secret.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ws import install, jobs, osinfo, output
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


def cmd_notify_send(args: argparse.Namespace) -> int:
    kind = "digest" if args.digest else "push"
    return _notify(lambda cfg: ntfy.send(cfg, os.environ, args.message, args.title, kind))


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
    """Hook entry. Pushes the alert the runner's hook JSON on stdin asks for. Advice, not a gate: always exits 0, and
    says on stderr why nothing went when something is wrong."""
    try:
        roles = (load(profile_path()) or {}).get("roles")
        cfg = roles.get("notify") if isinstance(roles, dict) else None
        if not isinstance(cfg, dict) or cfg.get("adapter") != ntfy.NAME:
            return 0  # no phone alerts set up: nothing to do
        payload = None if sys.stdin.isatty() else json.loads(sys.stdin.read(MAX_PAYLOAD) or "null")
        line = ntfy.alert(cfg, os.environ, payload, args.runner)
        if line:
            print(f"ws: {line}", file=sys.stderr)
    except (ProfileError, ntfy.NotifyError, ValueError, OSError) as e:  # ValueError covers a payload that isn't JSON
        print(f"ws: alert not sent: {e}", file=sys.stderr)
    return 0


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
    p.add_argument("message")
    p.add_argument("--title", default="ws")
    p.add_argument("--digest", action="store_true", help="the one daily digest, not a push")
    p.set_defaults(func=cmd_notify_send)
    p = ns.add_parser("status", help="today's count against the caps")
    p.set_defaults(func=cmd_notify_status)
    p = ns.add_parser("alert", help='hook entry: push "<folder> needs you" or "<folder> finished" (hook JSON on stdin)')
    p.add_argument("--runner", required=True, help="the runner whose hook is calling, for example claude-code")
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
