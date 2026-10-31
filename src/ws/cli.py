"""The `ws` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ws import (
    __version__,
    broker,
    canary,
    devices,
    doctor,
    gate,
    install,
    mcp_server,
    memory,
    modules,
    notes,
    notesdetect,
    osinfo,
    rating,
    roles,
    run,
    secrets,
    session,
    skills,
)
from ws import globals as ws_globals
from ws.profile import ProfileError, load, profile_path, write_if_changed

YES = {"y", "yes", "true", "1", "on"}
NO = {"n", "no", "false", "0", "off"}


def _yes_no(text: str) -> bool:
    t = text.strip().lower()
    if t in YES:
        return True
    if t in NO:
        return False
    raise ValueError(f"answer yes or no, not {text!r}")


def _ask(prompt: str, default: str, interactive: bool) -> str:
    if not interactive:
        return default
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def cmd_doctor(args: argparse.Namespace) -> int:
    osi = osinfo.detect()
    path = profile_path()
    checks = doctor.run_checks(osi, path, network=not args.offline)
    if args.json:
        print(json.dumps(doctor.report(checks, osi, path), indent=2))
    else:
        print(doctor.render(checks, osi))
    return 1 if any(c.status in doctor.BAD for c in checks) else 0


def cmd_init(args: argparse.Namespace) -> int:
    """The three install questions (ADR 0004): stays on? a server? which notes folder? Then write [machine]."""
    osi = osinfo.detect()
    if not osi.supported:
        print("ws: native Windows is not supported; install WSL2 and run ws inside it", file=sys.stderr)
        return 1
    path = profile_path()
    try:
        prof = load(path) or {}
    except ProfileError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    machine = prof.get("machine") or {}
    if not isinstance(machine, dict):
        print(f"ws: {path}: [machine] must be a table", file=sys.stderr)
        return 1
    machine = dict(machine)
    interactive = not args.yes and sys.stdin.isatty()
    try:
        if args.stays_on is not None:
            stays_on = _yes_no(args.stays_on)
        else:
            current = "yes" if machine.get("stays_on") else "no"
            stays_on = _yes_no(_ask("Does this machine stay on all the time?", current, interactive))
        server = (
            args.server
            if args.server is not None
            else _ask("A server to connect to (address, or none)?", machine.get("server") or "none", interactive)
        )
        server = "" if server.strip().lower() in ("", "none") else server.strip()
        notes = (
            args.notes
            if args.notes is not None
            else _ask("Your notes folder?", machine.get("notes") or notesdetect.suggest()[0], interactive)
        )
    except (ValueError, EOFError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 2
    notes_dir = Path(notes).expanduser()
    if not notes_dir.is_dir():
        try:
            create = args.create_notes or (
                interactive and _yes_no(_ask(f"{notes_dir} does not exist. Create it?", "yes", True))
            )
        except (ValueError, EOFError) as e:
            print(f"ws: {e}", file=sys.stderr)
            return 2
        if not create:
            print(f"ws: {notes_dir} is not a folder (pass --create-notes to make it)", file=sys.stderr)
            return 1
        notes_dir.mkdir(parents=True, exist_ok=True)
    if not notes.startswith("~"):
        notes = str(notes_dir.resolve())  # the same folder whichever directory doctor runs from
    machine.update({"os": osi.kind, "stays_on": stays_on, "server": server, "notes": notes})
    prof["machine"] = machine
    roles = prof.setdefault("roles", {})
    if not isinstance(roles, dict) or not isinstance(roles.setdefault("notes", {}), dict):
        print(f"ws: {path}: [roles] and [roles.notes] must be tables", file=sys.stderr)
        return 1
    roles["notes"]["path"] = notes
    try:
        changed = write_if_changed(path, prof)
    except ProfileError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(f"ws: {'wrote' if changed else '0 changes to'} {path}")
    print("next: `ws doctor` lists what is still to set up")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ws", description="Your memory, skills and rules, in any AI.")
    p.add_argument("--version", action="version", version=f"ws {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check this machine and every role; exit 1 if anything needs doing")
    d.add_argument("--json", action="store_true", help="machine-readable report")
    d.add_argument("--offline", action="store_true", help="skip checks that call a provider")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="answer the three setup questions for this machine")
    i.add_argument("--stays-on", metavar="yes|no", help="does this machine stay on all the time? (default no)")
    i.add_argument("--server", metavar="ADDRESS|none", help="a server to connect to (default none)")
    i.add_argument("--notes", metavar="PATH", help="your notes folder (default ~/Notes)")
    i.add_argument("--create-notes", action="store_true", help="create the notes folder if it is missing")
    i.add_argument("-y", "--yes", action="store_true", help="take the defaults for anything not given; no prompts")
    i.set_defaults(func=cmd_init)

    gate.register(sub)
    secrets.register(sub)
    broker.register(sub)
    notes.register(sub)
    memory.register(sub)
    mcp_server.register(sub)
    run.register(sub)
    ws_globals.register(sub)
    session.register(sub)
    install.register(sub)
    modules.register(sub)
    devices.register(sub)
    skills.register(sub)
    canary.register(sub)
    rating.register(sub)
    roles.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
