"""`ws connect`: walk the steps only a person can do to connect an integration, or show them to an agent.

`ws connect` lists the flows; `ws connect <id>` walks one in your own terminal; `--dry-run --json` prints each step as
`done` or `needs_human` (the agent contract: ws.connect.engine, docs/reference/connect.md), exit 0 when all done and 3
when a person has something to do; `--revoke` undoes the flow; `--diagnose` prints the step state with the config
redacted. The walk and `--revoke` refuse inside an agent session (the same check as the other person-only commands);
the list, `--dry-run` and `--diagnose` read state and change nothing, so an agent may run them. Captured secrets go to
the secrets store and nowhere else.
"""

from __future__ import annotations

import getpass
import os
import sys

COMMAND = "connect"

from ws import agentsession, output, paths  # noqa: E402
from ws import secrets as wsecrets  # noqa: E402
from ws.connect import engine, spec  # noqa: E402
from ws.output import WsError  # noqa: E402


class Terminal:
    """The person's terminal. A secret is read with no echo and never printed."""

    def say(self, text: str) -> None:
        print(text)

    def ask(self, label: str) -> str:
        return input(f"{label}: ")

    def ask_secret(self, label: str) -> str:
        return getpass.getpass(f"{label} (not shown): ")

    def pause(self, text: str) -> str:
        return input(f"{text}: ")


def _context(*, with_io: bool = False) -> engine.Context:
    try:
        reg = wsecrets.load_registry(wsecrets.registry_path())
    except wsecrets.SecretsError as e:
        raise WsError("registry", f"the secrets registry cannot be read: {e}", "fix it, then run this again") from e
    try:
        store = wsecrets.local_store()
    except wsecrets.SecretsError:
        store = None
    return engine.Context(
        state_dir=paths.state_dir(),
        store=store,
        registered=frozenset(reg),
        registry_path=str(wsecrets.registry_path()),
        io=Terminal() if with_io else None,
    )


def _flow(flow_id: str) -> spec.Flow:
    try:
        return spec.load(flow_id)
    except spec.FlowError as e:
        raise WsError("not_found", str(e), "`ws connect` lists the flows", 1) from e


def _person_only(verb: str, flow_id: str, *, needs_terminal: bool) -> None:
    why = agentsession.refusal(os.environ, sys.stdin.isatty(), sys.stdout.isatty())
    if why:
        raise WsError("refused", f"ws connect {flow_id} {verb}".rstrip() + f": {why}", "run it in your own terminal", 3)
    if needs_terminal and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise WsError("usage", f"ws connect {flow_id} needs a terminal", "run it where you can type", 2)


def _typed(e: engine.ConnectError) -> WsError:
    return WsError(e.kind, e.message, e.hint, engine.EXIT_ERROR)


def cmd_list(args) -> int:
    rows = []
    for fid in spec.ids():
        try:
            f = spec.load(fid)
            rows.append({"id": f.id, "title": f.title, "steps": len(f.steps)})
        except spec.FlowError as e:
            rows.append({"id": fid, "error": str(e)})
    if args.json:
        output.result({"flows": rows})
    else:
        for r in rows:
            print(f"{r['id']:<14} {r.get('title') or r.get('error')}")
        if not rows:
            print("no flows")
    return 0


def cmd_connect(args) -> int:
    target = args.target
    if target in (None, "list"):
        return cmd_list(args)
    flow = _flow(target)
    modes = [m for m in ("dry_run", "diagnose", "revoke") if getattr(args, m)]
    if len(modes) > 1:
        raise WsError("usage", "give one of --dry-run, --diagnose, --revoke", "", 2)
    try:
        if args.dry_run:
            data = engine.dry_run(_context(), flow)
            if args.json:
                output.result(data, status=output.OK if data["exit_code"] == 0 else output.ATTENTION)
            else:
                for r in data["steps"]:
                    print(f"{r['status']:<12} {r['id']:<16} {r.get('action', '')}")
                for b in data.get("blockers", []):
                    print(f"blocked: {b['kind']}\n{b['action']}")
            return data["exit_code"]
        if args.diagnose:
            data = engine.diagnose(_context(), flow)
            if args.json:
                output.result(data)
            else:
                for k, v in data.items():
                    print(f"{k}: {v}")
            return 0
        if args.revoke:
            _person_only("--revoke", flow.id, needs_terminal=False)
            data = engine.revoke(_context(), flow)
            if args.json:
                output.result(data)
            else:
                for line in data["did"] + data["tell"]:
                    print(line)
                if not data["did"] and not data["tell"]:
                    print("nothing to undo")
            return 0
        _person_only("", flow.id, needs_terminal=False)
        if args.json:
            raise WsError(
                "usage",
                "the walk is for a person at a terminal",
                f"an agent uses `ws connect {flow.id} --dry-run --json`",
                2,
            )
        _person_only("", flow.id, needs_terminal=True)
        res = engine.walk(_context(with_io=True), flow)
        return 0 if res["status"] == "done" else engine.EXIT_NEEDS_HUMAN
    except engine.ConnectError as e:
        raise _typed(e) from e


def register(sub) -> None:
    p = sub.add_parser(
        "connect", help="connect an integration: the steps only you can do, walked once, the same way each time"
    )
    p.add_argument("target", nargs="?", help="a flow id, or `list` (the default)")
    p.add_argument(
        "--dry-run", action="store_true", help="show each step as done or needs_human; with --json, the agent contract"
    )
    p.add_argument("--diagnose", action="store_true", help="step state and what was learned, with the nonce redacted")
    p.add_argument("--revoke", action="store_true", help="undo the flow: delete its secret, say what to undo elsewhere")
    p.set_defaults(func=cmd_connect)
