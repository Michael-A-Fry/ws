"""`ws setup`: the first-run and re-run setup, walked in your terminal (the default) or in a local page (`--web`).

Both screens ask the same questions through `flow`, and everything they change goes through `ws.install` (a plan, then
an apply), so what setup writes is what `ws install` would write, and a second run reports 0 changes. This file is the
command; `flow` holds the facts, the choices and their checks; `inline` is the terminal walk; `web` is the page;
`ws.brand` is the look they share.

Visual kit (the rules the terminal walk and the page both follow; the colours are the tokens in `ws.brand`):

- Violet (`accent`) is what you are on or have chosen: the cursor, a ticked plugin, the keys in a hint line, the filled
  part of a track. Mint (`ok`) is what is done, added or found, and the word "setup" in the banner. Amber (`warn`) is a
  warning, a file ws changed (`~`) or a fact it found nothing for. Red (`error`) is a refusal (`✗`). The name
  `ws-aitoolkit` is bold with no colour.
- Quiet text is one grey (`muted`); card borders are a second (`line2`); the empty part of a track is a third (`line`).
  Nothing else is grey.
- The mark is `ws` on a pixel grid, six rows tall, in a gradient from violet through indigo and teal to mint (the two
  between-hues appear only in the mark and the rule). Under 80 columns it folds to a three-row mark. The gradient rule
  under the banner runs the same way. On the page the rows are classes, with a darker ramp in light mode.
- Status block: one line per fact about this machine (machine, runner, model, budget, notes), label in grey, value
  after it, a mint tick where ws found something it can use and amber where it found nothing.
- Cards: each step is a rounded frame with a title on its left and a short caption on its right. Core is always on,
  Plugins are chosen, Review says what will be written, Install ticks the work off group by group.
- Hint line: the last line of anything that waits for a key, `key text` pairs in a row, the key in violet.
- Colour: off with a non-empty `NO_COLOR`, with `--no-color`, for output that isn't a terminal and for `TERM=dumb`
  (a non-empty `FORCE_COLOR` other than `0` turns it back on; `NO_COLOR` wins). Where the terminal's background isn't
  known to be dark (`COLORFGBG`), mint, amber, red and grey use the terminal's own basic colours so its theme can
  re-colour them; violet and the mark stay 256-colour.
- Fallbacks: with colour off the same words print with no colour code; a `TERM` of `dumb` prints each step once, with
  no cursor-move or erase code; with `--yes` nothing is asked and nothing redraws; under 80 columns the mark folds; a
  resize is picked up on the next draw.

Setup never writes a policy setting of its own: it writes only what the install plan gives it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from ws.output import WsError

HELP = """\
Usage:
  ws setup [--yes] [--web [--no-open]] [options]

  Sets ws up on this machine, or changes how it is set up. In a terminal it walks you through the steps and shows
  what it will write before it writes anything. Run it again any time: when nothing differs it says
  "Everything is set up: 0 changes" and offers to change something.

Flags:
  -y, --yes          take the defaults for anything not given; nothing is asked
  --web              do the same in a page in your browser (local to this machine)
  --no-open          with --web: print the address, do not open a browser
  --plugin NAME      switch a plugin on (repeat for more); see `ws plugin list`
  --notes PATH       your notes folder: ~/something or a full path
  --runner NAME      the AI tool to set up rules for: {runners}
  --cap USD          the most ws may spend in a month, e.g. 20
  --region CC        your country (two letters, e.g. NZ); kept in the profile
  --no-color         print no colour (a non-empty NO_COLOR does the same)
  -h, --help         show this help

Examples:
  ws setup                            walk through it in this terminal
  ws setup --yes                      take the defaults, ask nothing
  ws setup --web                      use a page in your browser
  ws setup --yes --region NZ --plugin finance --cap 20
  ws setup --notes ~/Obsidian/Vault   use an existing vault
"""

NO_HINT = ""  # an error from the install engine itself carries no hint; `flow.ChoiceError` carries its own


class _HandLaid(argparse.RawDescriptionHelpFormatter):
    """Prints only the description, which holds the whole layout: no generated usage line or flag list."""

    def add_usage(self, usage, actions, groups, prefix=None):
        pass

    def add_arguments(self, actions):
        pass


def _runner_names() -> list[str]:
    from ws import doctor

    return list(doctor.RUNNERS)


def register(sub) -> None:
    runners = _runner_names()
    p = sub.add_parser(
        "setup",
        help="set ws up on this machine, in your terminal or in a page (--web)",
        description=HELP.replace("{runners}", ", ".join(runners)),
        usage=argparse.SUPPRESS,
        formatter_class=_HandLaid,
    )
    p.add_argument("--web", action="store_true", help="use a local page in your browser instead of the terminal")
    p.add_argument("--no-open", action="store_true", help="with --web: print the address, do not open a browser")
    p.add_argument("-y", "--yes", action="store_true", help="take the defaults for anything not given; ask nothing")
    p.add_argument("--plugin", action="append", metavar="NAME", help="switch a plugin on (repeatable)")
    p.add_argument("--notes", metavar="PATH", help="your notes folder: ~/something or a full path")
    p.add_argument("--runner", choices=runners, help="the AI tool to set up rules for")
    p.add_argument("--cap", type=float, metavar="USD", help="the most ws may spend in a month")
    p.add_argument("--region", metavar="CC", help="your country (two letters); kept in [machine] region")
    p.add_argument("--no-color", action="store_true", help="print no colour (a non-empty NO_COLOR does the same)")
    p.set_defaults(func=cmd_setup, raw=_at_terminal)


def _at_terminal(args: argparse.Namespace) -> bool:
    """A command that keeps the terminal: the page prints its address, and the walk asks and draws."""
    return bool(args.web or (not args.yes and sys.stdin.isatty()))


def cmd_setup(args: argparse.Namespace) -> int:
    from ws.setup_ui import flow

    region = args.region
    if region is not None and not re.fullmatch(r"[A-Za-z]{2}", region):
        raise WsError(
            "bad_input",
            f"--region takes a two-letter country code such as NZ, not {region!r}",
            "for example: ws setup --region NZ",
        )
    try:
        facts = flow.gather(os.environ, region=region)
        choices = flow.choices_from(
            facts,
            plugins=args.plugin or (),
            notes=args.notes,
            runner=args.runner,
            cap=args.cap,
            region=region,
        )
        flow.check_choices(facts, choices)
    except flow.ENGINE_ERRORS as e:
        raise WsError("bad_input", flow.clean(e), getattr(e, "hint", NO_HINT)) from e
    if args.web:
        from ws.setup_ui import web

        return web.run(facts, choices, open_browser=not args.no_open)
    from ws.setup_ui import inline

    return inline.run(facts, choices, yes=args.yes, no_color=args.no_color)
