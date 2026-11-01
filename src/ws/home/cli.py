"""`ws home data`: the home snapshot (`ws.home/1`) as text, or as the JSON a script, an app or a page reads."""

from __future__ import annotations

import argparse
import sys

from ws import output
from ws.home import api
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


def register(sub) -> None:
    h = sub.add_parser("home", help="the home view: sessions, asks, evals and plugins from every source")
    hs = h.add_subparsers(dest="home_command", required=True)
    d = hs.add_parser("data", help="the home snapshot: one read-only view of every source (add --json for machines)")
    d.set_defaults(func=cmd_data)
