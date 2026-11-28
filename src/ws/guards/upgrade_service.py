"""upgrade-service: a session does not start, stop, change or imitate the unattended upgrade.

Why. `ws upgrade --unattended` runs with no one watching, from a systemd user timer, and installs a release or puts the
last one back. If a session could start that service, enable its timer, or run its entry itself, "the upgrade runs
outside every session" would mean nothing: the gate that guards ws would be installing ws. The timer is the switch
(DEC-131), and only the person turns it on, from a terminal of his own.

What is refused, for every runner:

- `systemctl` with a unit named `ws-upgrade*` (`.service`, `.timer` or bare) and any verb that
  changes something: start, stop, restart, enable, disable, edit, kill, mask and the rest. Reading them (`status`, `show`,
  `cat`, `list-timers`, `is-active` and the like) is allowed. A mutating `systemctl --user` (or `--global`, or `-M`)
  is refused too when the unit it names can't be read here: a variable or substitution, a glob or a brace in the name
  (`'ws-u*.timer'`, `ws-?pgrade.timer`: the manager expands it, this check does not), or a unit list fed in by `xargs` or
  `parallel`; `systemctl` is found wherever it stands in the words (`find -exec`, `systemd-run`);
- `systemctl --user set-environment`, `import-environment` and `unset-environment` (and under `--global` or `-M`): they
  put variables (`PYTHONPATH`) into every unit the user manager starts. `dbus-update-activation-environment --systemd` is
  the same verb. `daemon-reload` is allowed (a session installs timers of its own): a reload can only make the upgrade
  refuse, because the service checks at start that systemd loaded it from the text ws wrote, with no drop-in and no
  reload pending (`ws.upgradeunit.self_check`);
- the bus routes to the same manager: `busctl call` or `set-property`, `gdbus call`, `dbus-send` and `qdbus` that name
  `org.freedesktop.systemd1` (reading it, as `busctl tree` or `get-property` does, is allowed);
- `systemd-run` naming such a unit, and any command that has the words `upgrade` and `--unattended` (or a prefix of it
  that the option parser would still read), however ws is started (`ws`, `python -m ws`, `uv run`,
  inside `bash -c`).

The unit files, their drop-ins and the `*.target.wants/` folders (where an enable is a symlink) are protected from a
session by the gate's protected paths (`ws.userconfig`).

Swap note: this guard is the whole deny; a different init system or scheduler means a different list of verbs and
names in `_UNITS` and `_VERBS`.
"""

from __future__ import annotations

import fnmatch
import os
import re

from ws import policy
from ws.guards import Finding, known_keys

NAME = "upgrade-service"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10  # protects the machine

_UNITS = ("ws-upgrade*",)
_VERBS = frozenset(
    {"start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart", "kill"}
    | {"enable", "disable", "reenable", "mask", "unmask", "edit", "revert", "link", "set-property", "isolate"}
    | {"reset-failed", "clean", "freeze", "thaw", "preset", "add-wants", "add-requires"}
)
_ENV_VERBS = frozenset({"set-environment", "import-environment", "unset-environment"})
_FLAGS = ("--unattended",)
_DYNAMIC = ("$", "`")
_GLOB = frozenset("*?[{")  # a glob or a brace the manager or the shell expands into names this check does not see
_USER_MANAGER = frozenset({"--user", "--global", "-M"})  # --machine= is checked by prefix
_FED = re.compile(r"(?<![\w./-])(?:xargs|parallel)(?![\w.-])")  # the unit names arrive on a pipe
_BUS = "org.freedesktop.systemd1"
_BUS_TOOLS = {"busctl": {"call", "set-property"}, "gdbus": {"call"}, "dbus-send": None, "qdbus": None}  # None: any use
_WHY = (
    "{what} is refused: the unattended upgrade runs outside every session, so a session may not start, stop, enable, "
    "change or imitate it. Its timer finds each new release by itself once the person has switched it on; the switch "
    "is his, from a terminal of his own"
)


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def _unit(word: str) -> bool:
    base = os.path.basename(word.rsplit("=", 1)[-1]).split("@", 1)[0].lower()  # `--unit=NAME` names it too
    return any(fnmatch.fnmatch(base, pat) for pat in _UNITS)


def _flag(word: str) -> bool:
    """`--unattended`: the whole flag, or a prefix of it from four characters (`--unat`). The parser takes only the whole flag (`allow_abbrev=False`); the prefixes are refused as well in case that changes."""
    name = word.split("=", 1)[0]
    return name in _FLAGS or (len(name) >= 4 and name.startswith("--") and any(f.startswith(name) for f in _FLAGS))


def _user_manager(rest: list[str]) -> bool:
    return any(w in _USER_MANAGER or w.startswith("--machine") for w in rest)


def _systemctl(rest: list[str], fed: bool) -> Finding | None:
    """The words after `systemctl`: the upgrade units named, the verbs that change the manager itself, and the unit names
    this check can't read."""
    verb = next((w for w in rest if w in _ENV_VERBS), None)
    if verb and _user_manager(rest):
        return Finding("deny", _WHY.format(what=f"systemctl {verb}"))
    verbs = {w for w in rest if w in _VERBS}
    if not verbs:
        return None  # status, show, cat, list-timers and the like only read
    if any(_unit(w) for w in rest):
        return Finding("deny", _WHY.format(what=f"systemctl {min(verbs)} on the upgrade unit"))
    if not _user_manager(rest):
        return None
    names = [w for w in rest if not w.startswith("-")]
    if any(_GLOB & set(w) for w in names):
        return Finding(
            "deny",
            _WHY.format(what=f"systemctl {min(verbs)} on a unit name with a glob or a brace in it")
            + " (write each unit's name out)",
        )
    if any(any(d in w for d in _DYNAMIC) for w in rest):
        return Finding(
            "deny",
            _WHY.format(what=f"systemctl {min(verbs)} on a unit name this check can't read")
            + " (write the unit's name out)",
        )
    if fed:
        return Finding(
            "deny",
            _WHY.format(what=f"systemctl {min(verbs)} with its units fed in by xargs or parallel")
            + " (write the unit's name out)",
        )
    return None


def _bus(prog: str, words: list[str]) -> Finding | None:
    """A call to the user manager over the bus is `systemctl` by another name: refused for the tools that make one."""
    if prog not in _BUS_TOOLS or not any(_BUS in w for w in words):
        return None
    only = _BUS_TOOLS[prog]
    if only is None or any(w in only for w in words):
        return Finding("deny", _WHY.format(what=f"{prog} calling the systemd user manager"))
    return None


def judge(call, cfg, ctx) -> Finding | None:
    fed = bool(_FED.search(call.value))
    for words, _env in policy.simple_commands_env(call.value):
        if not words:
            continue
        prog = os.path.basename(words[0])
        if any(_flag(w) for w in words[1:]) and "upgrade" in words[1:]:
            return Finding("deny", _WHY.format(what="ws upgrade --unattended"))
        if prog == "systemd-run" and any(_unit(w) for w in words[1:]):
            return Finding("deny", _WHY.format(what="systemd-run naming the upgrade unit"))
        if prog == "dbus-update-activation-environment" and "--systemd" in words[1:]:
            return Finding("deny", _WHY.format(what="dbus-update-activation-environment --systemd"))
        found = _bus(prog, words[1:])
        if found:
            return found
        for i, word in enumerate(words):  # `systemctl` leads, or follows `find -exec`, `systemd-run`, `watch`
            if os.path.basename(word) == "systemctl":
                found = _systemctl(words[i + 1 :], fed)
                if found:
                    return found
    return None
