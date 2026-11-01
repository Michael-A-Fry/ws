"""`ws home`: the home page on loopback, `ws home --render FILE` to write it as one file, and `ws home data` for the
snapshot (`ws.home/1`) as text or as the JSON a script, an app or a page reads."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ws import output
from ws.home import api, server
from ws.home.records import Snapshot
from ws.style import Style, plural


def _lines(snap: Snapshot, st: Style) -> list[str]:
    waiting = sum(1 for s in snap.sessions if s.state == "waiting")
    opaque = sum(1 for s in snap.sessions if s.opaque) + sum(1 for a in snap.asks if a.opaque)
    lines = [
        st.heading(f"ws home on {snap.host or 'this machine'}"),
        *st.kv(
            [
                ("lead", snap.lead.name or snap.lead.id if snap.lead else "none set"),
                ("sessions", f"{len(snap.sessions)} ({waiting} waiting)"),
                ("asks", f"{len(snap.asks)} waiting for you"),
                ("evals", plural(len(snap.evals), "arm")),
                ("events", str(len(snap.events))),
                ("opaque", f"{opaque} hidden"),
            ],
            2,
        ),
        "",
        st.heading("sources"),
    ]
    for row in snap.sources:
        mark = st.status("ok" if row.ok else "skip", "read" if row.ok else row.kind, 14)
        lines.append(f"  {row.name:<14} {mark} {row.n if row.ok else row.why}")
    return lines


def cmd_data(args: argparse.Namespace) -> int:
    snap = api.snapshot()
    if args.json:
        output.result(snap.to_dict())
        return 0
    st = Style.detect(sys.stdout, no_color=getattr(args, "no_color", False))
    st.write("\n".join(_lines(snap, st)))
    return 0


def cmd_home(args: argparse.Namespace) -> int:
    """Serve the page on loopback, or (`--render FILE`) write it as one file and say where."""
    if not args.render:
        return server.run(open_browser=not args.no_browser)
    env = api.Env.make()
    text = server.page_for(env, live=False)
    out = Path(args.render).expanduser()
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)  # the page holds the person's own session names and folders
    if args.json:
        output.result({"path": str(out), "bytes": len(text.encode("utf-8"))})
    else:
        print(f"ws home: wrote {out}")
    return 0


def register(sub) -> None:
    h = sub.add_parser(
        "home",
        help="the home page: sessions, asks and evals from every source (--render FILE writes it as one file)",
    )
    h.add_argument("--render", metavar="FILE", help="write the page to FILE as it is now, and stop")
    h.add_argument("--no-browser", action="store_true", help="print the address and don't open a browser")
    h.set_defaults(func=cmd_home, raw=lambda a: not a.render)
    hs = h.add_subparsers(dest="home_command")
    d = hs.add_parser("data", help="the home snapshot: one read-only view of every source (add --json for machines)")
    d.set_defaults(func=cmd_data, raw=False)
