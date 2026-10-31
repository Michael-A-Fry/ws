"""Modules: the parts of ws you can switch on or off. `memory` and `notes` are on by default; the rest are opt-in and
not built yet, so switching one on only records the choice in `[modules]` of the profile.

Each module is a small adapter: what it is, whether it is on by default, the profile tables it owns and the folders it
needs. `ws module add|remove` goes through the same install.plan/apply as `ws install`, so it is idempotent too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace

from ws import install
from ws.install import Answers, InstallError, Step

NOT_BUILT = "not built yet"


@dataclass(frozen=True)
class Module:
    name: str
    what: str
    default_on: bool
    table: str  # the profile table it adds, written the way the profile shows it
    built: bool = False
    role_names: tuple[str, ...] = ()  # the [roles.*] tables it owns: dropped when it is switched off

    @property
    def status(self) -> str:
        return "built" if self.built else NOT_BUILT

    def roles(self, answers: Answers, env) -> dict[str, dict]:
        """The [roles.*] keys this module sets in the profile."""
        return {}

    def steps(self, answers: Answers, env) -> list[Step]:
        """The folders and files this module needs. Opt-in stubs need none."""
        return []


class Memory(Module):
    def roles(self, answers, env):
        return {"memory": {"path": install.store_path(answers.memory, env)}}

    def steps(self, answers, env):
        return [Step(install.expand(answers.memory, env), "dir", None, "your memory folder")]


class Notes(Module):
    def roles(self, answers, env):
        return {"notes": {"path": install.store_path(answers.notes, env)}}

    def steps(self, answers, env):
        notes = install.expand(answers.notes, env)
        return [Step(notes, "dir", None, "your notes folder"), Step(notes / "_inbox", "dir", None, "the notes _inbox")]


REGISTRY: dict[str, Module] = {
    m.name: m
    for m in (
        Memory(
            "memory", "what ws remembers: Markdown files in git, searchable", True, "roles.memory", True, ("memory",)
        ),
        Notes(
            "notes", "capture to the _inbox of your own notes folder (any app)", True, "roles.notes", True, ("notes",)
        ),
        Module("mail-drafts", "draft replies to your mail for you to send", False, "roles.mail-drafts", False),
        Module("calendar", "read your calendar to plan around", False, "roles.calendar", False),
        Module("finance", "your investments and spending, kept on your machine", False, "roles.finance", False),
        Module("knowledge-graph", "links between your notes and memory", False, "roles.knowledge-graph", False),
    )
}


def default_on() -> frozenset[str]:
    return frozenset(n for n, m in REGISTRY.items() if m.default_on)


def cmd_list(args: argparse.Namespace) -> int:
    try:
        on = install.answers_from_profile(os.environ).modules
    except InstallError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    rows = [
        {
            "name": m.name,
            "on": m.name in on,
            "default": m.default_on,
            "status": m.status,
            "what": m.what,
            "table": m.table,
        }
        for m in REGISTRY.values()
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        note = "" if r["status"] == "built" else f" ({r['status']})"
        print(f"{r['name']:<16} {'on ' if r['on'] else 'off'}  {r['what']}{note}")
    return 0


def _change(name: str, turn_on: bool) -> int:
    if name not in REGISTRY:
        print(f"ws: unknown module {name!r}; known: {', '.join(REGISTRY)}", file=sys.stderr)
        return 1
    try:
        if not install.profile_path().is_file():
            raise InstallError("no profile yet: run `ws install` first")
        answers = install.answers_from_profile(os.environ)
        modules = answers.modules | {name} if turn_on else answers.modules - {name}
        result = install.apply(install.plan(replace(answers, modules=modules)))
    except (InstallError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(f"ws: {name} is {'on' if turn_on else 'off'}: {result.summary()}")
    if turn_on and not REGISTRY[name].built:
        print(f"ws: {name} is {NOT_BUILT}: this records your choice and changes nothing else")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    return _change(args.name, True)


def cmd_remove(args: argparse.Namespace) -> int:
    return _change(args.name, False)


def register(sub) -> None:
    m = sub.add_parser("module", help="switch parts of ws on or off")
    ms = m.add_subparsers(dest="module_command", required=True)
    ls = ms.add_parser("list", help="every module, and whether it is on")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)
    for name, fn, text in (("add", cmd_add, "switch a module on"), ("remove", cmd_remove, "switch a module off")):
        p = ms.add_parser(name, help=text)
        p.add_argument("name", metavar="NAME")
        p.set_defaults(func=fn)
