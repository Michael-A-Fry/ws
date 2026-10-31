"""`ws setup`: the first-run and re-run setup, inline in your terminal (default) or in a page in your browser (`--web`).

Both forms walk the same steps (Welcome, Core, Plugins, Notes, Review, Install) over the same engine: `flow` reads the
real machine and calls `ws.install.plan` and `apply`, so nothing here writes a file itself. Run again on a set-up
machine it opens on "Everything is set up: 0 changes" and says what to change.

    inline.py   the terminal walk: plain ANSI and termios (imported late, so native Windows still loads `ws --help`)
    web.py      the page: stdlib http.server on 127.0.0.1, one token, closes itself when done
    flow.py     facts, choices, preview, install: the only module that talks to the engine
    brand.py    the colours, the mark, the rule and the progress track

Visual kit (the look Michael approved in taste-gate round 3; a new screen applies these rules and nothing else):

    Brand pair   Violet 99 (#875fff) means chosen, current, the name and a key you can press. Mint 49 (#00ffaf)
                 means done, added, found: ticks, "+N files", "found". Amber 214 (#ffaf00) means look at this: a
                 warning or a refusal. Grey 245 is borders and quiet text (an SGR "dim" is not used: some terminals
                 ignore it). Never a fourth colour. Dark neutral ground in the terminal and the dark page; the light
                 page flips the neutrals only.
    The mark     "ws" drawn on a pixel grid, each pixel two columns by one row, 6 rows by 34 columns, one colour per
                 row from `brand.GRADIENT` (violet, then indigo, teal, a mint finish). It sweeps in from the left.
                 Under 80 columns it folds to 3 rows (`brand.LOGO_SMALL`) and the tagline goes. Bold white name and
                 dim version beside it, "setup" in mint, then one gradient rule (`Ansi.rule`) the width of the cards.
    Status block Rows of a dim 9-column label and a bright value, two spaces in from the edge, straight from the real
                 machine: Machine, Runner, Model, Budget, Notes. A mint tick leads a thing that is found or ready;
                 amber is a value that needs doing. A spend meter appears only once there is spend.
    Hint line    One line under a step: the key in bold violet, what it does dim, separated by dim dots:
                 `↵ begin · q quit`. Keys: ↵ goes on, q or Esc cancels (before anything is written), space chooses,
                 ↑↓ move, d shows detail, b goes back.
    Cards        Rounded frames `╭─ Title ─── caption ─╮`: the title bold violet, the caption dim and right-aligned,
                 the border grey, rows inside padded one space. A card is as wide as the terminal less 4, at most
                 100 and at least 56. One card per step; a finished card is redrawn in place with its answer.
    Progress     ✓ (bold mint) is done, a braille spinner (`brand.SPINNER`, violet) is working, a dim dot is waiting.
                 A step that is running turns its spinner a little before the tick lands. Overall progress is one
                 continuous track (`Ansi.track`), violet and a dark remainder, with "n of m steps" dim after it.
                 Counts are mint ("+3 files"); a changed file is amber (~).
    Close        A bold mint "✓ Done" and the headline ("+N files, M changed", or "0 changes"), then one dim "Next"
                 line of violet commands to copy.
    Fallbacks    No terminal or `--yes`: nothing is asked, nothing animates, the same cards print once. `NO_COLOR` or
                 TERM=dumb: the same text with no escape codes. Ctrl-C, q or Esc before Install writes nothing.
    Web          The same pair, mark and words in the page; cards have 12-14 px radius and a 1 px line; chosen is a
                 violet border on a faint violet tint; the Install button is the one filled violet control; a fixed
                 bottom bar carries the live "+N files, M changed" in mint. Dark and light follow the system, with a
                 toggle. No external fonts, scripts or images.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from ws import doctor
from ws.output import WsError


def _fail(message: str, hint: str = ""):
    """An error, then what to try (the way claw-code ends its errors with a Usage line)."""
    raise WsError("bad_input", message, hint)


HINTS = (  # (a word in the message, what to try)
    ("unknown plugin", "see them with `ws plugin list`"),
    ("NZ only", "if that is wrong: ws setup --region NZ"),
    ("above 0", "for example: ws setup --cap 20"),
    ("too broad", "pick a folder of its own, for example: ws setup --notes ~/Notes"),
    ("must start with", "write it as ~/Notes or a full path, for example: ws setup --notes ~/Notes"),
    ("control character", "type a plain path"),
    ("unknown runner", "known runners: " + ", ".join(doctor.RUNNERS)),
)


def _at_terminal(args: argparse.Namespace) -> bool:
    """A person walking the steps (or the page): the command keeps the terminal, so --json has nothing to wrap."""
    return bool(args.web or (not args.yes and sys.stdin.isatty()))


def cmd_setup(args: argparse.Namespace) -> int:
    from ws.setup_ui import flow

    env = os.environ
    if args.region and not re.fullmatch(r"[A-Za-z]{2}", args.region):
        return _fail(
            f"--region takes a two-letter country code such as NZ, not {args.region!r}",
            "for example: ws setup --region NZ",
        )
    try:
        facts = flow.gather(env, region=args.region)
        choices = flow.choices_from(
            facts,
            plugins=args.plugin or (),
            notes=args.notes,
            runner=args.runner,
            cap=args.cap,
            region=args.region,
        )
        flow.check_choices(facts, choices)
    except flow.ENGINE_ERRORS as e:
        return _fail(str(e), next((hint for word, hint in HINTS if word in str(e)), ""))
    if args.web:
        from ws.setup_ui import web

        return web.run(facts, choices, open_browser=not args.no_open)
    from ws.setup_ui import inline

    return inline.run(facts, choices, yes=args.yes)


USAGE = """\
Usage:
  ws setup                        Walk through the steps in this terminal
  ws setup --web                  The same steps in a page in your browser
  ws setup --yes [FLAGS]          No prompts: take what ws finds and the flags below

Run it again any time: on a machine that is already set up it says "Everything is set up: 0 changes".
Nothing is written until you confirm at the review; Ctrl-C or q before that writes nothing.
"""
EXAMPLES = """\
Examples:
  ws setup
  ws setup --web --no-open
  ws setup --yes --plugin calendar --notes ~/Notes/Vault
  ws setup --yes --runner claude-code --cap 20
  ws setup --yes --region NZ --plugin finance
  ws doctor
"""


def register(sub) -> None:
    s = sub.add_parser(
        "setup",
        help="set up ws, step by step; run it again and it reports 0 changes (--web for a page in your browser)",
        description=USAGE,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage=argparse.SUPPRESS,
    )
    g = s.add_argument_group("Flags")
    g.add_argument("--web", action="store_true", help="a page in your browser instead of the terminal")
    g.add_argument("--no-open", action="store_true", help="with --web, print the address and do not open a browser")
    g.add_argument("-y", "--yes", action="store_true", help="no prompts: take the defaults and the flags below")
    g.add_argument(
        "--plugin", action="append", metavar="NAME", help="switch a plugin on; repeat for more (ws plugin list)"
    )
    g.add_argument("--notes", metavar="PATH", help="your notes folder (default: a vault found, else ~/Notes)")
    g.add_argument("--runner", choices=list(doctor.RUNNERS), help="the AI tool ws runs jobs with")
    g.add_argument("--cap", type=float, metavar="USD", help="your own monthly spend cap")
    g.add_argument("--region", metavar="CC", help="your country (two letters); kept in [machine] region")
    s.set_defaults(func=cmd_setup, raw=_at_terminal)
