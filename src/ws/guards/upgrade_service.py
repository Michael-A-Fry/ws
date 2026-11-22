"""upgrade-service: a session does not start, stop, change or imitate the unattended upgrade.

Why. `ws upgrade --unattended` and `--rescue` run with no one watching, from two systemd user units, and install a release
or go back one. If a session could start that service, or run its entry itself, "the upgrade runs outside every session"
would mean nothing: the gate that guards ws would be installing ws. The service is asked for an upgrade the one allowed
way (a request file it checks for itself), and everything else about it is the person's.

What is refused, for every runner:

- `systemctl` with a unit named `ws-upgrade*` or `ws-rescue*` (`.service`, `.timer`, `.path` or bare) and any verb that
  changes something: start, stop, restart, enable, disable, edit, kill, mask and the rest. Reading them (`status`, `show`,
  `cat`, `list-timers`, `is-active` and the like) is allowed. A mutating `systemctl --user` whose unit is a variable or a
  substitution is refused too, because this check can't see which unit it names;
- `systemd-run` naming such a unit, and any command that has the words `upgrade` and `--unattended` or `--rescue` (or
  a prefix of either flag that the option parser would still read), however ws is started (`ws`, `python -m ws`, `uv run`,
  inside `bash -c`).

The unit files are protected from a session by the gate's protected paths (`ws.userconfig`), and the request file is not:
it holds a tag or the word `rollback`, which the service checks as data.

Swap note: this guard is the whole deny; a different init system or scheduler means a different list of verbs and
names in `_UNITS` and `_VERBS`.
"""

from __future__ import annotations

import fnmatch
import os

from ws import policy
from ws.guards import Finding, known_keys

NAME = "upgrade-service"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10  # protects the machine

_UNITS = ("ws-upgrade*", "ws-rescue*")
_VERBS = frozenset(
    {"start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart", "kill"}
    | {"enable", "disable", "reenable", "mask", "unmask", "edit", "revert", "link", "set-property", "isolate"}
    | {"reset-failed", "clean", "freeze", "thaw", "preset", "add-wants", "add-requires"}
)
_FLAGS = ("--unattended", "--rescue")
_DYNAMIC = ("$", "`")
_WHY = (
    "{what} is refused: the unattended upgrade and its rescue timer run outside every session, so a session may not "
    "start, stop, change or imitate them. Ask for an upgrade by writing a request (a tag or `rollback`) where the lead "
    "does, and leave the rest to the person who owns the machine"
)


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def _unit(word: str) -> bool:
    base = os.path.basename(word.rsplit("=", 1)[-1]).split("@", 1)[0].lower()  # `--unit=NAME` names it too
    return any(fnmatch.fnmatch(base, pat) for pat in _UNITS)


def _flag(word: str) -> bool:
    """`--unattended` or `--rescue`, or a prefix of either that argparse still takes (`--unatt`, `--resc`)."""
    name = word.split("=", 1)[0]
    return len(name) >= 4 and name.startswith("--") and any(f.startswith(name) for f in _FLAGS)


def judge(call, cfg, ctx) -> Finding | None:
    for words, _env in policy.simple_commands_env(call.value):
        if not words:
            continue
        prog = os.path.basename(words[0])
        if any(_flag(w) for w in words[1:]) and "upgrade" in words[1:]:
            return Finding("deny", _WHY.format(what="ws upgrade --unattended or --rescue"))
        if prog == "systemd-run" and any(_unit(w) for w in words[1:]):
            return Finding("deny", _WHY.format(what="systemd-run naming the upgrade or rescue unit"))
        if prog != "systemctl":
            continue
        rest = words[1:]
        verbs = {w for w in rest if w in _VERBS}
        if not verbs:
            continue  # status, show, cat, list-timers and the like only read
        if any(_unit(w) for w in rest):
            return Finding("deny", _WHY.format(what=f"systemctl {min(verbs)} on the upgrade or rescue unit"))
        if "--user" in rest and any(any(d in w for d in _DYNAMIC) for w in rest):
            return Finding(
                "deny",
                _WHY.format(what=f"systemctl {min(verbs)} on a unit name this check can't read")
                + " (write the unit's name out)",
            )
    return None
