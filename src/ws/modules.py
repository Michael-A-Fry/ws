"""Plugins (the modules in the code): the parts of ws you can switch on or off. `memory`, `notes` and `skills` are on by
default and are the plugins that are built; `ws plugin list`, setup and `ws plugin add` offer only built plugins.

The registry also holds plugins still being developed (`built=False`). They are hidden from every list and choice. A
profile that already switches one on keeps loading; `ws doctor` (WS009) says it does nothing yet. Developers can show
them by setting `WS_SHOW_UNBUILT=1`.

Each is a small adapter: what it is, whether it is on by default, the profile tables it owns, the folders it needs and
the regions it is for. `ws plugin list|add|remove` goes through the same install.plan/apply as `ws install`, so it is
idempotent too. (`ws module` still works as a hidden alias.) A plugin names its own regions (ISO country codes) in its
metadata, and one made for some regions will not switch on in another. There is no default country: a plugin with no
regions is for everyone, and setup asks for a region only when an offered plugin has a region rule. The region is
`[machine] region` in the profile (`ws plugin add NAME --region CC` sets it), else the country of the time zone, read
from the system's `zone.tab` or, when that is missing, from the REGIONS table below. A locale alone is only a hint (a
UTC server often has a US one): it warns and carries on. If nothing says, nothing is refused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ws import install, output
from ws.install import Answers, InstallError, Step
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path, write_if_changed

NOT_BUILT = "not built yet"
SHOW_UNBUILT = "WS_SHOW_UNBUILT"  # developers only: set to 1 to list and offer plugins that are still being built


@dataclass(frozen=True)
class Module:
    name: str
    what: str
    default_on: bool
    table: str  # the profile table it adds, written the way the profile shows it
    built: bool = False
    role_names: tuple[str, ...] = ()  # the [roles.*] tables it owns: dropped when it is switched off
    region: tuple[str, ...] = ()  # ISO country codes it is made for (its own rule); empty = any

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
            "the skills ws ships (align, checkpoint, taste-gate, dataviz, resume, ship), added to your runner",
            True,
            "modules.skills",
            True,
        ),
        Module("mail-drafts", "draft replies to your mail for you to send", False, "roles.mail-drafts", False),
        Module("calendar", "read your calendar to plan around", False, "roles.calendar", False),
        Module("finance", "your investments and spending, kept on your machine", False, "roles.finance", False),
        Module("knowledge-graph", "links between your notes and memory", False, "roles.knowledge-graph", False),
    )
}


def default_on() -> frozenset[str]:
    return frozenset(n for n, m in REGISTRY.items() if m.default_on)


def show_unbuilt(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get(SHOW_UNBUILT) == "1"


def offered(env=None) -> dict[str, Module]:
    """The plugins a person is shown and may switch on: the built ones (all of them when a developer asks)."""
    show = show_unbuilt(env)
    return {n: m for n, m in REGISTRY.items() if m.built or show}


def unbuilt_on(on, env=None) -> list[str]:
    """Names in `on` that the registry holds but does not offer, in registry order."""
    shown = offered(env)
    return [n for n in REGISTRY if n in on and n not in shown]


# ---- region ----------------------------------------------------------------------------------------------------------

ZONE_TAB = Path("/usr/share/zoneinfo/zone.tab")


@dataclass(frozen=True)
class Region:
    """One row of the region table: how a time zone points at a country when the system's `zone.tab` can't be read."""

    code: str  # ISO 3166 alpha-2
    zones: tuple[str, ...]  # IANA zone names, with the legacy aliases people still have in $TZ
    abbreviations: tuple[str, ...] = ()  # POSIX names such as NZST; only ones no other country uses


def _regions(*rows: Region) -> dict[str, Region]:
    return {r.code: r for r in rows}


# A starting set, not a list of supported countries: `zone.tab` covers every country, and a profile or `--region`
# names any two-letter code. Adding a row needs no other change.
REGIONS: dict[str, Region] = _regions(
    Region(
        "AU",
        (
            "Australia/Sydney",
            "Australia/Melbourne",
            "Australia/Brisbane",
            "Australia/Adelaide",
            "Australia/Perth",
            "Australia/Darwin",
            "Australia/Hobart",
            "Australia/Canberra",
        ),
        ("AEST", "AEDT", "ACST", "ACDT", "AWST"),
    ),
    Region(
        "CA",
        (
            "America/Toronto",
            "America/Vancouver",
            "America/Edmonton",
            "America/Winnipeg",
            "America/Halifax",
            "America/St_Johns",
            "Canada/Eastern",
            "Canada/Pacific",
        ),
    ),
    Region("DE", ("Europe/Berlin",)),
    Region("FR", ("Europe/Paris",)),
    Region("GB", ("Europe/London", "Europe/Belfast", "GB")),
    Region("IE", ("Europe/Dublin", "Eire")),
    Region("IN", ("Asia/Kolkata", "Asia/Calcutta")),
    Region("JP", ("Asia/Tokyo", "Japan"), ("JST",)),
    Region(
        "NZ",
        ("Pacific/Auckland", "Pacific/Chatham", "NZ", "NZ-CHAT", "Antarctica/McMurdo", "Antarctica/South_Pole"),
        ("NZST", "NZDT"),
    ),
    Region("SG", ("Asia/Singapore", "Singapore")),
    Region(
        "US",
        (
            "America/New_York",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "America/Phoenix",
            "America/Anchorage",
            "Pacific/Honolulu",
            "US/Eastern",
            "US/Central",
            "US/Mountain",
            "US/Pacific",
        ),
    ),
    Region("ZA", ("Africa/Johannesburg",), ("SAST",)),
)
_BY_ZONE = {z: r.code for r in REGIONS.values() for z in r.zones}
_BY_ABBREVIATION = {a: r.code for r in REGIONS.values() for a in r.abbreviations}
_POSIX_TZ = re.compile(r"^([A-Za-z]{3,6})[-+]?\d")  # a POSIX $TZ such as NZST-12NZDT,M9.5.0,M4.1.0/3


def needs_region(env=None) -> bool:
    """Does any plugin on offer carry a region rule? Setup asks for a region only when one does."""
    return any(m.region for m in offered(env).values())


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
    try:
        for line in ZONE_TAB.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 3 and not line.startswith("#") and cols[2] == zone:
                return cols[0]
    except OSError:
        pass
    if hit := _BY_ZONE.get(zone):
        return hit
    posix = _POSIX_TZ.match(zone)
    return _BY_ABBREVIATION.get(posix.group(1).upper()) if posix else None


def zone_region(env) -> str | None:
    """The country of this machine's time zone (Europe/Dublin -> IE; a POSIX NZST-12NZDT -> NZ); None for UTC and any
    zone that belongs to no country."""
    zone = _zone_name(env)
    return _zone_country(zone) if zone else None


def locale_region(env) -> str | None:
    """The country of this machine's locale (en_NZ -> NZ). Only a hint: a UTC server often has a US locale."""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        locale = (env.get(key) or "").split(".")[0].split("@")[0]
        if len(locale) == 5 and locale[2] == "_" and locale[3:].isalpha():
            return locale[3:].upper()
    return None


def region_of(env) -> tuple[str | None, str | None]:
    """(country code, where it came from): 'profile' ([machine] region), 'zone' (the time zone) or 'locale', else
    (None, None). The first two are firm enough to refuse on; a locale is only a hint."""
    try:
        machine = (load(profile_path(env)) or {}).get("machine")
    except ProfileError:
        machine = None
    r = machine.get("region") if isinstance(machine, dict) else None
    if isinstance(r, str) and r.strip():
        return r.strip().upper(), "profile"
    if cc := zone_region(env):
        return cc, "zone"
    if cc := locale_region(env):
        return cc, "locale"
    return None, None


def _region_check(m: Module, region: str | None = None) -> tuple[str | None, str | None]:
    """(refusal, warning), each one plain line or None. A plugin made for other regions is refused when the person's
    profile (or `--region`) or this machine's time zone puts them elsewhere; a locale alone only warns, and an unknown
    region says nothing: ws will not refuse on a guess."""
    if not m.region:
        return None, None
    here, source = (region, "profile") if region else region_of(os.environ)
    if here is None or here in m.region:
        return None, None
    want = "/".join(m.region)
    fix = f"if that is wrong, run `ws plugin add {m.name} --region {m.region[0]}`"
    if source == "locale":
        return (
            None,
            f"{m.name} is for {want} only and your locale says {here}, which is only a guess; carrying on ({fix})",
        )
    where = "you said" if region else "your profile says" if source == "profile" else "this machine's time zone is in"
    return f"{m.name} is for {want} only and {where} {here}; {fix}", None


def _set_region(region: str) -> bool:
    """Write [machine] region to the profile. True if the file changed."""
    path = profile_path()
    prof = load(path) or {}
    machine = prof.get("machine")
    machine = dict(machine) if isinstance(machine, dict) else {}
    machine["region"] = region
    return write_if_changed(path, {**prof, "machine": machine})


# ---- the commands ----------------------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    try:
        on = install.answers_from_profile(os.environ).modules
    except InstallError as e:
        raise WsError("bad_config", str(e), "run `ws doctor`") from e
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
        for m in offered().values()
    ]
    if args.json:
        output.result(rows)
        return 0
    for r in rows:
        note = "" if r["status"] == "built" else f" ({r['status']})"
        only = "" if r["region"] == "any" else f" [{'/'.join(r['region'])} only]"
        print(f"{r['name']:<16} {'on ' if r['on'] else 'off'}  {r['what']}{only}{note}")
    return 0


def _change(name: str, turn_on: bool, region: str | None = None, output_json: bool = False) -> int:
    shown = offered()
    if name not in (shown if turn_on else REGISTRY):  # a plugin that is on but not offered can still be switched off
        raise WsError(
            "not_found",
            f"unknown plugin {name!r}",
            output.did_you_mean(name, shown) or f"known: {', '.join(shown)}",
        )
    if region is not None and not re.fullmatch(r"[A-Za-z]{2}", region):
        raise WsError("bad_input", f"--region takes a two-letter country code such as CA, not {region!r}")
    region = region.upper() if region else None
    try:
        if not install.profile_path().is_file():
            raise InstallError("no profile yet: run `ws install` first")
        refusal, warning = _region_check(REGISTRY[name], region) if turn_on else (None, None)
        if refusal:
            raise InstallError(refusal)
        if warning:
            print(f"ws: {warning}", file=sys.stderr)
        if region and _set_region(region):
            print(f"ws: region set to {region} in your profile")
        answers = install.answers_from_profile(os.environ)
        modules = answers.modules | {name} if turn_on else answers.modules - {name}
        result = install.apply(install.plan(replace(answers, modules=modules)))
    except (InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    if output_json:
        output.result({"name": name, "on": turn_on, "summary": result.summary()})
        return 0
    print(f"ws: {name} is {'on' if turn_on else 'off'}: {result.summary()}")
    if turn_on and not REGISTRY[name].built:
        print(f"ws: {name} is {NOT_BUILT}: this records your choice and changes nothing else")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    return _change(args.name, True, args.region, args.json)


def cmd_remove(args: argparse.Namespace) -> int:
    return _change(args.name, False, output_json=args.json)


def register(sub) -> None:
    """`ws plugin` is the documented command; `ws module` does the same and is hidden from the help."""
    for command, shown in (("plugin", True), ("module", False)):
        m = sub.add_parser(command, help="switch parts of ws on or off" if shown else argparse.SUPPRESS)
        ms = m.add_subparsers(dest="module_command", required=True)
        ls = ms.add_parser("list", help="every plugin, and whether it is on")
        ls.set_defaults(func=cmd_list)
        for name, fn, text in (("add", cmd_add, "switch a plugin on"), ("remove", cmd_remove, "switch a plugin off")):
            p = ms.add_parser(name, help=text)
            p.add_argument("name", metavar="NAME")
            if name == "add":
                p.add_argument("--region", metavar="CC", help="your country (two letters); kept in [machine] region")
            p.set_defaults(func=fn)
