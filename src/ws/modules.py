"""Plugins (the modules in the code): the parts of ws you can switch on or off. `memory`, `notes` and `skills` are on by
default; the rest are opt-in and not built yet, so switching one on only records the choice in `[modules]` of the profile.

Each is a small adapter: what it is, whether it is on by default, the profile tables it owns, the folders it needs and
the regions it is for. `ws plugin list|add|remove` goes through the same install.plan/apply as `ws install`, so it is
idempotent too. (`ws module` still works as a hidden alias.) A plugin made for one region, such as `finance` for NZ,
will not switch on in another: the region is `[machine] region` in the profile, else a guess from the time zone and
locale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ws import install
from ws.install import Answers, InstallError, Step
from ws.profile import ProfileError, load, profile_path

NOT_BUILT = "not built yet"


@dataclass(frozen=True)
class Module:
    name: str
    what: str
    default_on: bool
    table: str  # the profile table it adds, written the way the profile shows it
    built: bool = False
    role_names: tuple[str, ...] = ()  # the [roles.*] tables it owns: dropped when it is switched off
    region: tuple[str, ...] = ()  # ISO country codes it is made for; empty = any

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
        return [Step(install.expand(answers.memory, env), "dir", None, "your memory folder", root=True)]


class Notes(Module):
    def roles(self, answers, env):
        return {"notes": {"path": install.store_path(answers.notes, env)}}

    def steps(self, answers, env):
        notes = install.expand(answers.notes, env)
        return [
            Step(notes, "dir", None, "your notes folder", root=True),
            Step(notes / "_inbox", "dir", None, "the notes _inbox"),
        ]


class Skills(Module):
    def steps(self, answers, env):
        from ws import skills  # imported late: ws.skills uses ws.install, which modules also needs

        out: list[Step] = []
        for runner in skills.runners_for(answers.runner, env):
            out += skills.steps(runner, env)
        return out


REGISTRY: dict[str, Module] = {
    m.name: m
    for m in (
        Memory(
            "memory", "what ws remembers: Markdown files in git, searchable", True, "roles.memory", True, ("memory",)
        ),
        Notes(
            "notes", "capture to the _inbox of your own notes folder (any app)", True, "roles.notes", True, ("notes",)
        ),
        Skills(
            "skills",
            "the skills ws ships (align, checkpoint, taste-gate, dataviz), added to your runner",
            True,
            "modules.skills",
            True,
        ),
        Module("mail-drafts", "draft replies to your mail for you to send", False, "roles.mail-drafts", False),
        Module("calendar", "read your calendar to plan around", False, "roles.calendar", False),
        Module(
            "finance",
            "your investments and spending, kept on your machine",
            False,
            "roles.finance",
            False,
            region=("NZ",),
        ),
        Module("knowledge-graph", "links between your notes and memory", False, "roles.knowledge-graph", False),
    )
}


def default_on() -> frozenset[str]:
    return frozenset(n for n, m in REGISTRY.items() if m.default_on)


# ---- region ----------------------------------------------------------------------------------------------------------

_NZ_ZONES = {"Pacific/Auckland", "Pacific/Chatham", "NZ", "NZ-CHAT", "Antarctica/McMurdo", "Antarctica/South_Pole"}
ZONE_TAB = Path("/usr/share/zoneinfo/zone.tab")


def _zone_name(env) -> str | None:
    """The machine's IANA time zone name, from $TZ, else /etc/localtime or /etc/timezone."""
    tz = (env.get("TZ") or "").lstrip(":")
    if tz:
        return tz
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _zone_country(zone: str) -> str | None:
    if zone in _NZ_ZONES:
        return "NZ"
    try:
        for line in ZONE_TAB.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 3 and not line.startswith("#") and cols[2] == zone:
                return cols[0]
    except OSError:
        pass
    return None


def guess_region(env) -> str | None:
    """The country this machine looks to be in: its time zone first, else the country of its locale (en_NZ -> NZ).
    None when neither says (UTC with a plain C locale). A guess only: `[machine] region` in the profile overrides it."""
    zone = _zone_name(env)
    if zone and (cc := _zone_country(zone)):
        return cc
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        locale = (env.get(key) or "").split(".")[0].split("@")[0]
        if len(locale) == 5 and locale[2] == "_" and locale[3:].isalpha():
            return locale[3:].upper()
    return None


def region_of(env) -> tuple[str | None, str]:
    """(country code or None, where it came from: 'profile' or 'guess')."""
    try:
        machine = (load(profile_path(env)) or {}).get("machine")
    except ProfileError:
        machine = None
    r = machine.get("region") if isinstance(machine, dict) else None
    if isinstance(r, str) and r.strip():
        return r.strip().upper(), "profile"
    return guess_region(env), "guess"


def _wrong_region(m: Module) -> str | None:
    """One plain line when this machine is known to be outside the plugin's regions; None otherwise (including when the
    region cannot be told: ws will not refuse on a guess it could not make)."""
    if not m.region:
        return None
    here, source = region_of(os.environ)
    if here is None or here in m.region:
        return None
    where = "your profile says" if source == "profile" else "this machine looks to be in"
    return (
        f"{m.name} is for {'/'.join(m.region)} only and {where} {here}; "
        "if that is wrong, set `region` under [machine] in your profile"
    )


# ---- the commands ----------------------------------------------------------------------------------------------------


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
            "region": list(m.region) or "any",
        }
        for m in REGISTRY.values()
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        note = "" if r["status"] == "built" else f" ({r['status']})"
        only = "" if r["region"] == "any" else f" [{'/'.join(r['region'])} only]"
        print(f"{r['name']:<16} {'on ' if r['on'] else 'off'}  {r['what']}{only}{note}")
    return 0


def _change(name: str, turn_on: bool) -> int:
    if name not in REGISTRY:
        print(f"ws: unknown plugin {name!r}; known: {', '.join(REGISTRY)}", file=sys.stderr)
        return 1
    try:
        if not install.profile_path().is_file():
            raise InstallError("no profile yet: run `ws install` first")
        if turn_on and (why := _wrong_region(REGISTRY[name])):
            raise InstallError(why)
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
    """`ws plugin` is the documented command; `ws module` does the same and is hidden from the help."""
    for command, shown in (("plugin", True), ("module", False)):
        m = sub.add_parser(command, help="switch parts of ws on or off" if shown else argparse.SUPPRESS)
        ms = m.add_subparsers(dest="module_command", required=True)
        ls = ms.add_parser("list", help="every plugin, and whether it is on")
        ls.add_argument("--json", action="store_true")
        ls.set_defaults(func=cmd_list)
        for name, fn, text in (("add", cmd_add, "switch a plugin on"), ("remove", cmd_remove, "switch a plugin off")):
            p = ms.add_parser(name, help=text)
            p.add_argument("name", metavar="NAME")
            p.set_defaults(func=fn)
