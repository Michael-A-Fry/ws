"""The `ws` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ws import (
    __version__,
    approvals,
    broker,
    canary,
    config_cmd,
    devices,
    doctor,
    extcmd,
    gate,
    install,
    mcp_server,
    memory,
    modules,
    notes,
    notesdetect,
    osinfo,
    output,
    rating,
    roles,
    run,
    secrets,
    session,
    setup_ui,
    skills,
    status,
    version,
)
from ws import eval as ws_eval
from ws import globals as ws_globals
from ws.output import WsError
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
    bad = any(c.status in doctor.BAD for c in checks)
    if args.json:
        output.result(doctor.report(checks, osi, path), status=output.ATTENTION if bad else output.OK)
    else:
        print(doctor.render(checks, osi))
    return 1 if bad else 0


def cmd_init(args: argparse.Namespace) -> int:
    """The three install questions (ADR 0004): stays on? a server? which notes folder? Then write [machine]."""
    osi = osinfo.detect()
    if not osi.supported:
        raise WsError("unsupported", "native Windows is not supported", "install WSL2 and run ws inside it")
    path = profile_path()
    try:
        prof = load(path) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {path}, then run `ws init` again") from e
    machine = prof.get("machine") or {}
    if not isinstance(machine, dict):
        raise WsError("bad_config", f"{path}: [machine] must be a table", f"fix {path}, then run `ws init` again")
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
        raise WsError(
            "bad_input", str(e), "answer yes or no, or pass the answers as flags (see `ws help init`)", 2
        ) from e
    notes_dir = Path(notes).expanduser()
    if not notes_dir.is_dir():
        try:
            create = args.create_notes or (
                interactive and _yes_no(_ask(f"{notes_dir} does not exist. Create it?", "yes", True))
            )
        except (ValueError, EOFError) as e:
            raise WsError("bad_input", str(e), "answer yes or no", 2) from e
        if not create:
            raise WsError("not_found", f"{notes_dir} is not a folder", "pass --create-notes to make it")
        notes_dir.mkdir(parents=True, exist_ok=True)
    if not notes.startswith("~"):
        notes = str(notes_dir.resolve())  # the same folder whichever directory doctor runs from
    machine.update({"os": osi.kind, "stays_on": stays_on, "server": server, "notes": notes})
    prof["machine"] = machine
    roles = prof.setdefault("roles", {})
    if not isinstance(roles, dict) or not isinstance(roles.setdefault("notes", {}), dict):
        raise WsError("bad_config", f"{path}: [roles] and [roles.notes] must be tables", f"fix {path}")
    roles["notes"]["path"] = notes
    try:
        changed = write_if_changed(path, prof)
    except ProfileError as e:
        raise WsError("io", str(e), f"check that {path.parent} is writable") from e
    if args.json:
        output.result({"path": str(path), "changed": changed, "next": "ws doctor"})
        return 0
    print(f"ws: {'wrote' if changed else '0 changes to'} {path}")
    print("first: `ws doctor` lists what is still to set up")
    return 0


class Parser(argparse.ArgumentParser):
    """argparse that raises `WsError` instead of exiting, with "did you mean" for commands and flags."""

    _hint = ""

    def add_subparsers(self, **kwargs):
        action = super().add_subparsers(**kwargs)
        add = action.add_parser

        def add_parser(name, **kw):
            p = add(name, **kw)
            p.set_defaults(ws_command=p.prog.removeprefix("ws ").replace(" ", "."), ws_parser=p)
            return p

        action.add_parser = add_parser
        return action

    def _check_value(self, action, value):
        try:
            super()._check_value(action, value)
        except argparse.ArgumentError:
            self._hint = output.did_you_mean(str(value), action.choices)
            raise

    def error(self, message):
        hint = self._hint or f"run `{self.prog} --help`"
        raise WsError("usage", message, hint, 2)

    def parse_args(self, args=None, namespace=None):
        ns, extras = self.parse_known_args(args, namespace)
        if extras:
            where = getattr(ns, "ws_parser", self)
            flags = [o for a in where._actions for o in a.option_strings]
            bad = extras[0]
            hint = output.did_you_mean(bad.split("=")[0], flags) if bad.startswith("-") else ""
            raise WsError(
                "usage", f"unrecognized arguments: {' '.join(extras)}", hint or f"run `{where.prog} --help`", 2
            )
        return ns


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def _help_json(parser: argparse.ArgumentParser) -> dict:
    kids = []
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            kids = [{"name": c.dest, "help": c.help or ""} for c in a._choices_actions]
    options, positionals = [], []
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            continue
        if a.option_strings:
            options.append({"flags": a.option_strings, "metavar": a.metavar, "help": a.help or ""})
        else:
            positionals.append({"name": a.metavar or a.dest, "nargs": a.nargs, "help": a.help or ""})
    return {
        "command": parser.prog,
        "usage": parser.format_usage().removeprefix("usage: ").strip(),
        "description": parser.description or "",
        "commands": kids,
        "options": options,
        "positionals": positionals,
        "global_options": ["--json", "-C PATH"],
    }


def cmd_help(args: argparse.Namespace) -> int:
    parser = args.ws_root
    for word in args.topic:
        kids = _subcommands(parser)
        if word not in kids:
            raise WsError("usage", f"no help topic {word!r}", output.did_you_mean(word, kids) or "run `ws help`", 2)
        parser = kids[word]
    root = parser is args.ws_root
    extra = extcmd.help_lines(_subcommands(parser)) if root else []
    if args.json:
        data = _help_json(parser)
        if root:
            builtins = _subcommands(parser)
            data["external"] = extcmd.list_externals(builtins)
            data["fallback"] = extcmd._fallback_for_help()
        output.result(data)
    else:
        print(parser.format_help(), end="")
        if extra:
            print("\n" + "\n".join(extra))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="ws", description="Your memory, skills and rules, in any AI.")
    p.add_argument("--version", action="version", version=f"ws {__version__}")
    p.add_argument("--json", action="store_true", help="every command: one JSON envelope on stdout (or WS_OUTPUT=json)")
    p.add_argument("-C", "--cwd", metavar="PATH", help="every command: run as if started in PATH")
    p.set_defaults(ws_command="ws", ws_parser=p, ws_root=p)
    sub = p.add_subparsers(dest="command", required=True)

    h = sub.add_parser("help", help="help for a command: `ws help gate generate` (add --json for machines)")
    h.add_argument("topic", nargs="*", help="a command, then its subcommand")
    h.set_defaults(func=cmd_help)

    d = sub.add_parser("doctor", help="check this machine and every role; exit 1 if anything needs doing")
    d.add_argument("--offline", action="store_true", help="skip checks that call a provider")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="answer the three setup questions for this machine")
    i.add_argument("--stays-on", metavar="yes|no", help="does this machine stay on all the time? (default no)")
    i.add_argument("--server", metavar="ADDRESS|none", help="a server to connect to (default none)")
    i.add_argument("--notes", metavar="PATH", help="your notes folder (default ~/Notes)")
    i.add_argument("--create-notes", action="store_true", help="create the notes folder if it is missing")
    i.add_argument("-y", "--yes", action="store_true", help="take the defaults for anything not given; no prompts")
    i.set_defaults(func=cmd_init, raw=lambda a: not a.yes and sys.stdin.isatty())  # it asks the questions

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
    approvals.register(sub)
    setup_ui.register(sub)
    config_cmd.register(sub)
    extcmd.register(sub)
    status.register(sub)
    ws_eval.register(sub)
    version.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    kind, want_json = "ws", output.wants_json(raw)
    try:
        g, rest = output.split_global(raw)
        want_json = g.json
        parser = build_parser()
        flags = [o for a in parser._actions for o in a.option_strings]
        spec = extcmd.plan(raw, g, rest, _subcommands(parser), flags)
        if g.cwd is not None and (spec is None or spec.keep_cwd):
            output.enter_cwd(g.cwd)
        if spec is not None:
            return extcmd.launch(spec)
        args = parser.parse_args(rest)
        kind = args.ws_command
        return output.dispatch(args, kind, g.json)
    except WsError as e:
        if want_json:
            print(json.dumps(output.error_envelope(e, kind), indent=2))
        else:
            print(output.error_text(e), file=sys.stderr)
        return e.exit_code
