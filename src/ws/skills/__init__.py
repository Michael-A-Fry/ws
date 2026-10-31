"""The skills ws ships (`align`, `checkpoint`, `taste-gate`, `dataviz`) and how they get onto a machine.

Each is an Agent Skill: a folder with a `SKILL.md` that starts with `name` and `description` front matter, the open
format both Claude Code and OpenCode read. The text lives in one Python module per skill (`ws secret wall` copies only
`.py` files, so package data is a string); the install writes it out as `<runner skills folder>/<name>/SKILL.md`.

The install goes through the same `install.plan/apply` steps as everything else, so the manifest records each file and
`ws uninstall` removes it. A skill the person already has under that name is never touched: ws reports it and moves on.
That includes one a runner reads from another runner's folder (OpenCode reads `~/.claude/skills` and `~/.agents/skills`).

    ws skills [list] [--json]      each shipped skill, per runner: installed by ws, yours (kept), removed by you, or not
                                   installed
    ws skills add NAME             put a skill back that you removed (or add one that is not installed yet)

A skill ws installed and you then deleted stays deleted: ws takes that as your choice, says so on `ws install`, and
only `ws skills add NAME` brings it back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ws import adapters, install
from ws import globals as ws_globals
from ws.install import Step
from ws.skills import align, checkpoint, dataviz, taste_gate

SKILLS: dict[str, str] = {m.NAME: m.TEXT for m in (align, checkpoint, taste_gate, dataviz)}
FILE = "SKILL.md"
# Folders other than its own that a runner also reads skills from, relative to $HOME (OpenCode's documented list).
ALSO_READS = {"opencode": ("claude-code", "~/.agents/skills")}


@dataclass(frozen=True)
class Item:
    name: str
    runner: str
    path: Path  # where ws would put it: <skills folder>/<name>
    action: str  # "install" (new or ws's own) | "yours" (kept) | "shared" (read elsewhere) | "removed" (by the person)
    note: str = ""


def skills_dir(runner: str, env) -> Path:
    """Where a runner looks for the person's own skills."""
    return ws_globals.user_dir(adapters.get(runner).GLOBALS, env) / "skills"


def _also_reads(runner: str, env) -> list[Path]:
    out = []
    for where in ALSO_READS.get(runner, ()):
        out.append(skills_dir(where, env) if where in adapters.all_adapters() else install.expand(where, env))
    return out


def installed_runners(env) -> list[str]:
    """The runners that have a config folder on this machine."""
    found = []
    for name, mod in adapters.all_adapters().items():
        layout = getattr(mod, "GLOBALS", None)
        if layout and ws_globals.user_dir(layout, env).is_dir():
            found.append(name)
    return found


def roots(env) -> list[Path]:
    """Every folder ws could have put a shipped skill in: the places `ws uninstall` may remove from. Worked out from
    the shipped names, never from the manifest."""
    out = []
    for name, mod in adapters.all_adapters().items():
        if getattr(mod, "GLOBALS", None):
            out += [skills_dir(name, env) / s for s in SKILLS]
    return out


def _ours(path: Path, entries: dict[str, dict]) -> bool:
    e = entries.get(str(path / FILE))
    return bool(e and e.get("created_by_ws") is True)


def items(runners: list[str], env, entries: dict[str, dict] | None = None) -> list[Item]:
    """What to do with each shipped skill for each runner. `entries` is the install manifest's entries by path."""
    entries = install._read_manifest(env)[0] if entries is None else entries
    out = []
    for runner in runners:
        own = skills_dir(runner, env)
        elsewhere = _also_reads(runner, env)
        for name in SKILLS:
            target = own / name
            if _ours(target, entries) and (target / FILE).is_file():
                out.append(Item(name, runner, target, "install", "installed by ws"))
            elif _ours(target, entries):  # ws made it, and it has gone since: that was the person's doing
                out.append(
                    Item(name, runner, target, "removed", f"you removed it: `ws skills add {name}` brings it back")
                )
            elif target.exists() or target.is_symlink():
                out.append(Item(name, runner, target, "yours", f"you already have {target}: ws left it"))
            elif other := next((d / name for d in elsewhere if (d / name).exists()), None):
                out.append(Item(name, runner, target, "shared", f"{runner} already reads yours at {other}"))
            else:
                out.append(Item(name, runner, target, "install", "not installed yet"))
    return out


def steps(runner: str, env, add: str | None = None) -> list[Step]:
    """The install steps for one runner: a folder and a SKILL.md per skill that is new or already ws's own. A skill
    the person removed is left out, unless it is the one named in `add`."""
    out: list[Step] = []
    for it in items([runner], env):
        if it.action == "install" or (it.action == "removed" and it.name == add):
            out.append(Step(it.path, "dir", None, f"the {it.name} skill for {runner}", root=True))
            out.append(Step(it.path / FILE, "file", SKILLS[it.name], f"the {it.name} skill for {runner}"))
    return out


def runners_for(runner: str, env) -> list[str]:
    """The profile's runner, else every runner found on this machine."""
    return [runner] if runner in adapters.all_adapters() else installed_runners(env)


def notices(runner: str, env) -> list[str]:
    """One line per skill the person removed, for `ws install` to print (ws leaves it out each time)."""
    return [
        f"{it.name} ({it.runner}): {it.note}" for it in items(runners_for(runner, env), env) if it.action == "removed"
    ]


# ---- the command -----------------------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        runner = install.answers_from_profile(env).runner
        rows = items(runners_for(runner, env), env)
    except install.InstallError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{**vars(r), "path": str(r.path)} for r in rows], indent=2))
        return 0
    if not rows:
        print("ws: no runner found to put skills in; set one with `ws install --runner claude-code|opencode`")
        return 0
    for r in rows:
        state = {
            "install": r.note,
            "yours": "yours, kept",
            "shared": "yours, read from elsewhere",
            "removed": "removed by you",
        }[r.action]
        print(f"{r.name:<12} {r.runner:<12} {state}")
        if r.action != "install":
            print(f"{'':12} {'':12} {r.note}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        runner = install.answers_from_profile(env).runner
        runners = runners_for(runner, env)
        if not runners:
            print("ws: no runner found to put skills in; set one with `ws install --runner claude-code|opencode`")
            return 1
        for r in runners:
            (it,) = (i for i in items([r], env) if i.name == args.name)
            if it.action in ("yours", "shared"):
                print(f"ws: {it.note}")
                continue
            result = install.apply(steps(r, env, add=args.name))
            print(f"ws: {args.name} for {r}: {result.summary()}")
    except (install.InstallError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("skills", help="the skills ws ships, and where they are installed")
    ss = s.add_subparsers(dest="skills_command")
    ls = ss.add_parser("list", help="each skill, per runner")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)
    add = ss.add_parser("add", help="put a skill back (one you removed), or add one that is not installed yet")
    add.add_argument("name", choices=list(SKILLS))
    add.set_defaults(func=cmd_add)
    s.set_defaults(func=cmd_list, json=False)
