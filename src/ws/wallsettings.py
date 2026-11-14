"""What the sandbox settings Claude Code reads say about the wall: the keys that widen it, and `ws doctor`'s WS012.

In Claude Code the `sandbox` block of the settings is the wall for shell commands. ws writes the block, and Claude Code
merges it with every other settings file it reads, so a setting in your own file or in a managed-settings file can open
it again. This module finds those settings. `ws install` refuses to merge beside them unless you keep them on purpose
(`--allow-widened-sandbox`), and WS012 reports them, with a stale `denyRead` list, in place of "ok".

It reads files; it proves nothing about the host. A settings source ws cannot see (a `--settings` flag, a managed policy
held outside the files below) can still widen the wall, and WS012 says only what it can read.

A class with an `os_user` (see ws.policy.Class) has two files (ADR 0027). The managed-settings file is the one 0.1.9
wrote, unchanged: a drop-in in the managed-settings folder that Claude Code reads for every account on the machine, so a bare
`claude` run as the class account is bound by it. It holds the sandbox required, no unsandboxed retry, bypass off, writes in
the class's repos and scratch only, the home folder unreadable except those repos, inbound lead-only, and the network shut
but for the class's own hosts (`network.allowedDomains` with `allowManagedDomainsOnly`). The launch file is new: one per class,
root-owned, in a `ws-classes` folder beside the managed-settings folder, which Claude Code does not read. `ws gate launch`
reads it and merges it into the launch copy it passes with `--settings` (`merge`, `load_launch_file`), the launch keys
winning, so a session of another class or of none never gets it. It holds `Read` and `Edit` denied for the sealed paths,
file reads held to the working folders (`permissions.blockReadsOutsideWorkingDirectories`), cross-session messaging refused
(`crossSessionInbound`, a `SendMessage` and `ListAgents` deny, `isolatePeerMachines`), `disableAllHooks` false, the other
classes' repos unreadable, the class's hosts under `strictAllowlist`, the class marker and the OpenTelemetry content logs
off. Keys Claude Code reads only from managed settings (`MANAGED_ONLY`) are in neither file: from a `--settings` file they
do nothing, and in the machine-wide file they would bind every account. This module renders both as text (`class_settings`,
`launch_settings`, `install_text`, for `ws install --class`), judges the managed one (`class_rows`, the WS014 rows) and
checks what a project adds beyond the launch file (`project_problem`: its settings, `.mcp.json`, and the frontmatter of
its skills, agents and commands; a project's `sandbox` table may only narrow). It never writes either: they are placed by a person
with sudo, so ws prints them and nothing more. `MANAGED_FILES` and `MANAGED_DIRS` are the only places the managed-settings
paths, and the folder of launch files beside them, come from.

Python 3.11+, standard library only (`pwd` is imported inside the functions that need it).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ws import paths, policy
from ws.approvals import ApprovalError
from ws.approvals import _read_regular as read_regular
from ws.output import WsError

OK, WARN, FAIL = "ok", "warn", "fail"

# Where Claude Code reads managed settings (a file, and a folder of drop-ins). Tests point these at a temp folder.
MANAGED_FILES = (
    "/etc/claude-code/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",
)
MANAGED_DIRS = ("/etc/claude-code/managed-settings.d", "/Library/Application Support/ClaudeCode/managed-settings.d")

# `permissions.disableBypassPermissionsMode`: the one spelling of it, in the settings ws generates (launch and user
# config), in the class file and in the doctor rows. Claude Code accepts only the string "disable".
BYPASS_KEY = "disableBypassPermissionsMode"
BYPASS_OFF = "disable"
# `permissions.blockReadsOutsideWorkingDirectories` (Claude Code 2.1.257): the file tools refuse a read outside the
# working folders. In the launch settings and the launch file; never in a person's own settings file.
READ_BLOCK_KEY = "blockReadsOutsideWorkingDirectories"
# Its write twin (2.1.314): Write, Edit and NotebookEdit refuse a path outside the working folders, in every mode.
WRITE_BLOCK_KEY = "blockWritesOutsideWorkingDirectories"
# A call that would wait on a prompt is denied instead (2.1.319; user, managed or --settings only): headless jobs.
UNATTENDED_KEY = "denyInsteadOfPrompting"
# The credentials the sandbox keeps from shell commands. The settings reference gives 2.1.221 for its `mask` mode and
# no version for `deny`; 2.1.221 is the earliest it names, and the key is in every binary from 2.1.308 on.
CREDENTIALS_KEY = "sandbox.credentials"
# The settings keys the wall writes that only newer Claude Code versions read: dotted key -> first version that does.
# The one table `ws doctor` WS016 and the e2e skip read; a version is parsed as doctor._version_tuple parses it.
MIN_CLAUDE = {
    f"permissions.{READ_BLOCK_KEY}": (2, 1, 257),
    "sandbox.network.strictAllowlist": (2, 1, 219),  # a data class session only (claude_code.with_hosts)
    # the launch settings hide the home folder and re-allow the project (claude_code.deny_home). Claude Code 2.1.77 added
    # the key; 2.1.78 made absolute paths work in the sandbox's folder lists, which every entry written there is.
    "sandbox.filesystem.allowRead": (2, 1, 78),
    # a launch file only: cross-session messaging refused, and the cross-machine approval (any settings file)
    "crossSessionInbound": (2, 1, 224),
    "isolatePeerMachines": (2, 1, 224),
    f"permissions.{WRITE_BLOCK_KEY}": (2, 1, 314),
    f"permissions.{UNATTENDED_KEY}": (2, 1, 319),  # a headless job's settings only (claude_code.unattended)
    CREDENTIALS_KEY: (2, 1, 221),
}
INBOUND_ENV = "WS_INBOUND"  # the managed file's record of who may send in
INBOUND_VALUE = "lead-only"
WALL_MARKER = "WS_WALLED_CLASS"  # the launch file's env: this session is a walled class's (the hook reads it)
CROSS_KEY, CROSS_VALUE = (
    "crossSessionInbound",
    "refuse",
)  # Claude Code 2.1.224: a session drops what arrives from others
# The launch file sets `disableAllHooks` false: a project's true would switch off the gate hook, and a `--settings` value
# outranks a project's (the launch check also refuses a project that sets it).
HOOKS_KEY = "disableAllHooks"
PEER_KEY = (
    "isolatePeerMachines"  # belt only: SendMessage is denied, so this is the second wall for a cross-machine send
)
MESSAGING_DENY = ("SendMessage", "ListAgents")  # the real cross-session control: the tools are not there to call
# OpenTelemetry content logs: prompts and tool details stay out of any telemetry a class session sends (Claude Code reads
# the off value `0` from a --settings file, and a project file may only turn them off too). In the launch file.
OTEL_OFF = {"OTEL_LOG_USER_PROMPTS": "0", "OTEL_LOG_TOOL_DETAILS": "0", "OTEL_LOG_TOOL_CONTENT": "0"}
# Keys Claude Code honours only from managed settings (checked with its docs): ws does not write them. In a `--settings`
# file they do nothing, and in the machine-wide managed file they would switch off every account's own hooks and rules.
# Their work is done by the launch check (`project_problem`) and by `strictAllowlist`. Recorded in ADR 0027.
MANAGED_ONLY = (
    "allowManagedHooksOnly",
    "allowManagedPermissionRulesOnly",
    "sandbox.network.allowManagedDomainsOnly",
    "sandbox.filesystem.allowManagedReadPathsOnly",
)
CLASS_FILE_PREFIX = "50-ws-class-"
LAUNCH_FOLDER = "ws-classes"  # beside the managed-settings folder; Claude Code reads only the file and that folder
LOAD_FILES = (".mcp.json", ".claude/settings.json", ".claude/settings.local.json")  # what a project loads at the start
# `sandbox.enableWeakerNestedSandbox` and `sandbox.enableWeakerNetworkIsolation` (Claude Code's sandboxing reference): each
# weakens the sandbox for a tool that needs it. The launch file sets both false, and a project may not set either.
WEAKER_KEYS = ("enableWeakerNestedSandbox", "enableWeakerNetworkIsolation")

INSTALL = "sudo apt install bubblewrap socat (Debian, Ubuntu) or sudo dnf install bubblewrap socat (Fedora)"
FOR_FEWER = "run under `ws gate launch` for fewer refusals"
STRICT = "every command is read strictly, so a path ws can't read in full is refused"
OVERRIDE = "--allow-widened-sandbox"

_MAX_BYTES = 1 << 20


@dataclass(frozen=True)
class Widening:
    """One setting that opens the wall. `path` is its place in the settings file; `item` is the list entry (empty for a
    switch); `file` is the settings file it sits in."""

    path: tuple[str, ...]
    item: str
    file: Path
    why: str

    @property
    def key(self) -> str:
        return ".".join(self.path)

    @property
    def label(self) -> str:
        """The name ws records and doctor prints: the key, and the entry when it is one of a list."""
        return f"{self.key} {self.item}" if self.item else self.key


def managed_files() -> list[Path]:
    """The managed-settings files that exist here, in the order Claude Code would read them (the file, then the
    drop-ins by name)."""
    found: list[Path] = [Path(f) for f in MANAGED_FILES if os.path.isfile(f)]
    for d in MANAGED_DIRS:
        try:
            found += sorted(p for p in Path(d).iterdir() if p.suffix == ".json" and p.is_file())
        except OSError:
            continue
    return found


def read(path: Path) -> dict | None:
    """The settings in `path`, or None when it can't be read as a JSON object (Claude Code ignores such a file too)."""
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _home(env: Mapping[str, str]) -> str:
    return env.get("HOME") or str(Path.home())


def _norm(entry: object, home: str) -> str | None:
    """An absolute, normalised path for a settings entry (`~/x`, `$HOME/x`, `/x`), cut at its first wildcard so a glob is
    read as the folder it starts in. None when the entry isn't a path ws can resolve (a relative one, say)."""
    if not isinstance(entry, str) or not entry:
        return None
    text = entry
    for pre in ("${HOME}", "$HOME"):
        if text == pre or text.startswith(pre + "/"):
            text = "~" + text[len(pre) :]
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    if not text.startswith("/"):
        return None
    keep: list[str] = []
    for part in text.split("/"):
        if any(c in part for c in "*?["):
            break
        keep.append(part)
    return posixpath.normpath("/".join(keep) or "/")


def overlaps(a: str, b: str) -> bool:
    """True when one path is the other or sits inside it."""
    if a == b or a == "/" or b == "/":
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def expected_paths(block: Mapping, home: str) -> list[str]:
    """The paths the wall holds: the policy's `denyRead` paths and the `denyWrite` paths (ws's own files)."""
    fs = block.get("filesystem") if isinstance(block, Mapping) else None
    fs = fs if isinstance(fs, Mapping) else {}
    out = [_norm(p, home) for key in ("denyRead", "denyWrite") for p in fs.get(key, []) if isinstance(p, str)]
    return [p for p in dict.fromkeys(out) if p]


def widenings(doc: Mapping, file: Path, block: Mapping, home: str) -> list[Widening]:
    """The settings in `doc` (the content of `file`) that open the wall `block` describes. Allowlist-shaped: an
    `allowRead` or `allowWrite` entry that isn't a path ws can resolve counts as one, and so does an `allowRead` entry at
    or under the home folder (or above it): the launch settings hide the home and Claude Code merges this entry in."""
    sb = doc.get("sandbox") if isinstance(doc, Mapping) else None
    if not isinstance(sb, Mapping):
        return []
    found: list[Widening] = []

    def add(path: tuple[str, ...], item: str, why: str) -> None:
        found.append(Widening(path, item, file, why))

    if sb.get("enabled") is False:
        add(("sandbox", "enabled"), "", "turns the sandbox off, so no shell command runs inside the wall")
    ex = sb.get("excludedCommands")
    if ex:
        for c in ex if isinstance(ex, list) else [ex]:
            add(
                ("sandbox", "excludedCommands"),
                str(c),
                "runs this command outside the sandbox, with no file limits, so it can read and write what the wall hides",
            )
    if sb.get("allowUnsandboxedCommands") is True:
        add(
            ("sandbox", "allowUnsandboxedCommands"),
            "",
            "lets a command that fails in the sandbox run again outside it",
        )
    fs = sb.get("filesystem")
    if isinstance(fs, Mapping):
        if fs.get("disabled") is True:
            add(("sandbox", "filesystem", "disabled"), "", "switches off the sandbox's file limits")
        held = expected_paths(block, home)
        ours = {
            _norm(p, home) for p in (block.get("filesystem") or {}).get("allowWrite", [])
        }  # ws's own, from the policy
        for key in ("allowRead", "allowWrite"):
            entries = fs.get(key)
            for e in entries if isinstance(entries, list) else ([entries] if entries else []):
                p = _norm(e, home)
                if p is not None and p in ours:
                    continue
                if p is None:
                    add(
                        ("sandbox", "filesystem", key),
                        str(e),
                        "is not a path ws can read, so it may re-open a path the wall holds",
                    )
                elif any(overlaps(p, h) for h in held):
                    add(
                        ("sandbox", "filesystem", key),
                        str(e),
                        "re-opens a path the wall hides or keeps read-only (a deny_read path, or one of ws's own)",
                    )
                elif key == "allowRead" and overlaps(p, home):
                    add(
                        ("sandbox", "filesystem", key),
                        str(e),
                        "re-opens a folder under your home that a session started with `ws gate launch` hides: this "
                        "file merges into that session's settings, so it would be readable again",
                    )
    return found


def scan(
    settings: Path,
    block: Mapping,
    env: Mapping[str, str],
    *,
    managed: bool = True,
    own: Mapping[Path, dict] | None = None,
) -> list[Widening]:
    """The widening settings in your settings file and, with `managed`, in every managed-settings file ws can read. A
    managed file in `own` (a class file, by path) that holds exactly what ws renders for it is ws's own wall and is not
    read as a widening; one that differs is read like any other."""
    home = _home(env)
    found: list[Widening] = []
    for f in [settings, *(managed_files() if managed else [])]:
        doc = read(f)
        if doc is not None and not (own and own.get(f) == doc):
            found += widenings(doc, f, block, home)
    return found


def refusal(found: list[Widening], where: Path) -> WsError:
    """The refusal `ws install` gives when your settings widen the wall (kind `sandbox_widened`): each setting, its file and
    what it does; the hint names what to delete from which file, or the flag that keeps them on purpose."""
    lines = "".join(f"\n  {w.label} ({w.file}): {w.why}" for w in found)
    delete = "; ".join(f"`{key}` from {file}" for key, file in dict.fromkeys((w.key, w.file) for w in found))
    return WsError(
        "sandbox_widened",
        f"{where}: these settings widen the sandbox ws relies on, so ws changed nothing:{lines}",
        f"delete {delete}, then run `ws install` again; or keep them on purpose with `ws install {OVERRIDE}` "
        "(ws records the choice, and `ws doctor` then reports the wall as widened, never ok)",
    )


def host_problem(kind: str, which=None) -> str | None:
    """Why this machine cannot run Claude Code's sandbox, or None. Linux and WSL2 need bubblewrap and socat; macOS has
    its own (sandbox-exec); native Windows has none."""
    which = which or shutil.which
    if kind in ("linux", "wsl"):
        missing = [n for n in ("bwrap", "socat") if not which(n)]
        if missing:
            return f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed"
        return None
    if kind == "macos":
        return None if which("sandbox-exec") else "sandbox-exec was not found"
    return "this system has no sandbox (use macOS, Linux or WSL2)"


def _covered(path: str, have: list[str]) -> bool:
    return any(path == h or path.startswith(h.rstrip("/") + "/") for h in have)


def doctor_check(env: Mapping[str, str], kind: str) -> tuple[str, str, str]:
    """(status, detail, fix) for WS012: the wall's settings, read from the files. `ok` only when the settings file holds
    the sandbox block, its `denyRead` covers every deny_read path the wall can hide, and nothing in your settings or a
    managed-settings file widens it. It reads settings; it doesn't run a probe."""
    from ws import userconfig  # late: userconfig imports this module

    pol_file = policy.policy_path(dict(env))
    try:
        pol = policy.load(pol_file)
    except (policy.PolicyError, OSError):
        return WARN, f"the policy doesn't load, so the sandbox state is unknown; {STRICT}", f"fix {pol_file}"
    if not pol.gate.sandbox:
        return (
            WARN,
            f"unsandboxed: the policy sets sandbox = false under [gate]; {STRICT}",
            "set sandbox = true under [gate] in the policy, then run `ws install`",
        )
    try:
        runner, gate = userconfig._profile_runner(env)
    except Exception:  # noqa: BLE001 - a profile that doesn't read is WS003's to say
        runner, gate = "", ""
    problem = host_problem(kind)
    fix = f"install them: {INSTALL}; or set sandbox = false under [gate] in the policy"
    if problem:
        if kind not in ("linux", "wsl", "macos"):
            return WARN, f"unsandboxed: {problem}; {STRICT}", "install WSL2 and run ws inside it"
        in_file = runner == "claude-code" and gate == "every-session"
        tail = "Claude Code refuses to start with the sandbox on" if in_file else STRICT
        return (FAIL if in_file else WARN), f"unsandboxed: {problem}; {tail}", fix
    if runner and runner != "claude-code":
        return WARN, f"unsandboxed: {runner} has no sandbox of its own; {STRICT}; {FOR_FEWER}", ""
    if runner != "claude-code" or gate != "every-session":
        return OK, "sessions started with `ws gate launch` run in ws's own wall", ""
    where = userconfig.target(runner, env)
    try:
        doc, _ = userconfig.load(where)
    except WsError:  # WS008 says why the file doesn't read
        doc = {}
    have = doc.get("sandbox") if isinstance(doc, dict) else None
    if not (isinstance(have, dict) and have):
        return (
            WARN,
            f"unsandboxed: {where} has no sandbox block; {STRICT}; {FOR_FEWER}",
            "to switch it on, set sandbox = true under [gate] in the policy, then run `ws install`",
        )
    block = userconfig.expected_sandbox(pol, pol_file, env)
    problems: list[str] = []
    home = _home(env)
    fs = have.get("filesystem") if isinstance(have.get("filesystem"), dict) else {}
    held = [p for p in (_norm(x, home) for x in fs.get("denyRead", []) if isinstance(x, str)) if p]
    wanted = [p for p in (_norm(x, home) for x in (block.get("filesystem") or {}).get("denyRead", [])) if p]
    stale = [p for p in wanted if not _covered(p, held)]
    fix = ""
    if stale:
        problems.append(f"denyRead in {where} is stale: it lacks {len(stale)} of the policy's deny_read paths")
        fix = "run `ws install`"
    classes = class_files(pol, env, kind)
    found = scan(where, block, env, own={f: want for _, f, want in classes})
    mine = doc if isinstance(doc, dict) else {}
    for _, _, want in classes:  # a class file in place: what your own settings add beyond it widens it
        found += [w for w in class_widenings(mine, where, want, home) if w.label not in {x.label for x in found}]
    if found:
        problems.append("wall widened by user: " + "; ".join(f"{w.label} ({w.file})" for w in found))
        fix = fix or (
            "take those settings out (then `ws install` if you used " + OVERRIDE + "), or accept the wider wall"
        )
    if problems:
        return WARN, "; ".join(problems), fix
    return OK, f"the sandbox block is in {where} and covers every deny_read path the wall can hide", ""


# ---- a class with an OS user: its managed-settings file and its launch file -------------------------------------------

PARTS = (
    "OS user exists",
    "file present and root-owned",
    "sandbox required",
    "unsandboxed commands off",
    "bypass off",
    "write limits",
    "home deny-read",
    "inbound lead-only",
)


@dataclass(frozen=True)
class Row:
    """One part of one class, for the WS014 line: `status` is ok, warn or fail."""

    cls: str
    part: str
    status: str
    detail: str
    fix: str = ""


def passwd(name: str):
    """The user database entry for `name`, or None (no such user, or no user database on this system)."""
    try:
        import pwd  # here, not at the top: POSIX only
    except ImportError:
        return None
    try:
        return pwd.getpwnam(name)
    except (KeyError, ValueError, OverflowError):
        return None


def class_users(pol: policy.Policy) -> list[policy.Class]:
    """The classes that name an OS user, in name order."""
    return [c for _, c in sorted(pol.classes.items()) if c.os_user]


def keys_written(pol: policy.Policy) -> list[str]:
    """The keys of MIN_CLAUDE the wall writes under this policy: the read block unless `read_outside_project` is on (a
    launch file always has it), the write block unless `write_outside_project` is on, `allowRead` and the credentials
    list with a sandbox (the launch settings' deny-home and token deny), the no-prompt key every headless job gets, and
    `strictAllowlist` when the policy has data classes and a sandbox."""
    keys = []
    if not pol.gate.read_outside_project or class_users(pol):
        keys.append(f"permissions.{READ_BLOCK_KEY}")
    if not pol.gate.write_outside_project:
        keys.append(f"permissions.{WRITE_BLOCK_KEY}")
    keys.append(f"permissions.{UNATTENDED_KEY}")
    if pol.gate.sandbox:
        keys += ["sandbox.filesystem.allowRead", CREDENTIALS_KEY]
    if pol.gate.sandbox and pol.classes:
        keys.append("sandbox.network.strictAllowlist")
    if class_users(pol):
        keys += [CROSS_KEY, PEER_KEY]
    return keys


def class_file_name(name: str) -> str:
    return f"{CLASS_FILE_PREFIX}{name}.json"


def class_dir(kind: str) -> str:
    """The managed-settings folder a class file goes in on this system: the macOS one there, the first one elsewhere."""
    if kind == "macos" and len(MANAGED_DIRS) > 1:
        return MANAGED_DIRS[-1]
    return MANAGED_DIRS[0]


def class_file_path(name: str, kind: str) -> Path:
    return Path(class_dir(kind)) / class_file_name(name)


def ws_drop_ins(kind: str) -> list[Path]:
    """The ws class drop-ins in place in this system's managed-settings folder (`class_dir`), sorted by name. Claude Code
    merges every drop-in there for every account on the machine, so a second one binds accounts it was not written for.
    Only the folder is listed; nothing is read, and a folder that can't be listed is an empty answer."""
    try:
        names = os.listdir(class_dir(kind))
    except OSError:
        return []
    return [Path(class_dir(kind)) / n for n in sorted(names) if drop_in_class(n) is not None]


def drop_in_class(file_name: str) -> str | None:
    """The class a drop-in file name (`class_file_name`) is for, or None for any other file."""
    if not (file_name.startswith(CLASS_FILE_PREFIX) and file_name.endswith(".json")):
        return None
    name = file_name[len(CLASS_FILE_PREFIX) : -len(".json")]
    return name if name and class_file_name(name) == file_name else None


def drop_in_warning(found: Iterable[Path], pol: policy.Policy | None, own: str | None = None) -> str | None:
    """One sentence for a machine whose managed-settings folder carries more class drop-ins than the tested shape (one
    class per machine), or one for a class the policy doesn't name; None when it is as tested. `own` is the class being set
    up: its drop-in is not counted against it. Class names only, never a path."""
    names = sorted({c for p in found if (c := drop_in_class(p.name)) is not None})
    others = [c for c in names if c != own]
    unknown = [c for c in others if pol is not None and c not in pol.classes]
    if not (others and (own is not None or len(names) > 1)) and not unknown:
        return None
    shown = ", ".join(others)
    why = f"no class of this policy: {', '.join(unknown)}; " if unknown else ""
    return (
        f"class drop-ins for {shown} are in place in the managed-settings folder, and Claude Code applies every one to "
        f"every account on this machine ({why}one class per machine is the tested shape)"
    )


def launch_file_name(name: str) -> str:
    return f"{name}.json"


def launch_dir(kind: str) -> str:
    """The folder launch files live in: `ws-classes` beside the managed-settings folder, which Claude Code does not read."""
    return str(Path(class_dir(kind)).parent / LAUNCH_FOLDER)


def launch_file_path(name: str, kind: str) -> Path:
    """Where a class's launch file is. The one source of the place: the launch (`load_launch_file`), `ws walls check` and the
    text `ws install --class` prints all ask here. The managed-settings file is `class_file_path`."""
    return Path(launch_dir(kind)) / launch_file_name(name)


def scratch_dirs(kind: str) -> tuple[str, ...]:
    return ("/tmp", "/private/tmp") if kind == "macos" else ("/tmp",)


def class_home(cls: policy.Class, kind: str) -> str:
    """The home folder a `~` in the class's repos means, and the one printed step 1 closes: the class user's own when the
    system knows the account, else the folder the printed `useradd` or `sysadminctl` makes for it (`/home/NAME`,
    `/Users/NAME`). Never the home of whoever runs ws: the file and the `chown` must name the same folders before and
    after the account exists."""
    user = cls.os_user or ""
    entry = passwd(user)
    if entry is not None and entry.pw_dir:
        return entry.pw_dir
    return f"/Users/{user}" if kind == "macos" else f"/home/{user}"


def class_roots(cls: policy.Class, home: str) -> list[str]:
    """The folders the class's `repos` name, absolute: a folder, or a folder followed by a trailing `/**`. A bare name or
    a pattern that starts with a wildcard names no folder and is left out. A wildcard in a folder's name (`work/ops-*`,
    `work/*`, `*/work`) would put the folder above the repos in the file and in the `chown`, so it is a usage error."""
    roots: list[str] = []
    for glob in cls.repos:
        text = glob.removesuffix("/**")
        if _norm(text, home) is None:
            continue
        if any(c in text for c in "*?["):
            raise WsError(
                "usage",
                f"class {cls.name!r} has a repo with a wildcard in a folder name ({glob!r}), which a settings file "
                "and a chown cannot hold without the folder above the repos",
                f"edit `repos` under [classes.{cls.name}] in {policy.policy_path()}: name each repo folder, or the "
                'folder that holds them as "/path/**"; then run `ws install --class '
                f"{cls.name}` again",
                2,
            )
        root = _norm(text, home)
        if root and root != "/":
            roots.append(root)
    return list(dict.fromkeys(roots))


def _rule_path(path: str) -> str:
    """A path as a Claude Code permission rule spells it: `~/x` is the home folder, `//x` an absolute path."""
    return f"/{path}" if path.startswith("/") else path


def sealed_paths(
    cls: policy.Class,
    home: str,
    kind: str,
    pol: policy.Policy | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """The paths a session of `cls` may neither read nor edit, as permission-rule paths: the home folder (`~/**`; when a
    repo of the class sits inside it, only what it keeps out of sight: the dot-folders `~/.*/**` and the top-level
    dotfiles `~/.*`, such as `~/.claude.json` and the shell start files, which `~/.*/**` alone misses), ws's own folders
    as ws.paths names them for that home (state, config, data, cache, the legacy folder, and the folder `env` names
    outright, such as `WS_HOME`), and the other classes' repos when the policy is given. The home and the other classes'
    repos match the sandbox's `denyRead` (`launch_settings` carries the others'). Never spells a ws folder by hand."""
    inside = any(_within(r, home) for r in class_roots(cls, home))
    sealed = ["~/.*/**", "~/.*"] if inside else ["~/**"]
    sealed += [f"{_home_rule(str(d), home)}/**" for d in _ws_folders(home, env)]
    sealed += [f"{_rule_path(o)}/**" for o in _other_roots(cls, home, kind, pol)]
    return list(dict.fromkeys(sealed))


def _ws_folders(home: str, env: Mapping[str, str] | None) -> tuple[Path, ...]:
    """ws's folders in every layout for an account whose home is `home` (ws.paths decides which), and the folders `env`
    names outright when it sets any (`paths.OVERRIDE_VARS`: `WS_HOME` and the `WS_*_DIR` variables). Nothing else of `env`
    is read: its `XDG_*` values are the printing account's, not the class account's."""
    seen = {"HOME": home}
    if env:
        seen |= {k: env[k] for k in paths.OVERRIDE_VARS if env.get(k)}
    return paths.protected_roots(seen)


def _home_rule(path: str, home: str) -> str:
    """A permission-rule path: `~/x` for a path under `home`, else the absolute `//x`."""
    base = home.rstrip("/")
    return "~" + path[len(base) :] if path.startswith(base + "/") else _rule_path(path)


def _other_roots(cls: policy.Class, home: str, kind: str, pol: policy.Policy | None) -> list[str]:
    """The repo folders of every other walled class of `pol` (one with an `os_user`: a class with none has no account whose
    home its `~/` repos could mean), each resolved against its own class's home, leaving out any that overlaps a folder of
    `cls` itself (sealing it would seal the class's own repos)."""
    if pol is None:
        return []
    own = class_roots(cls, home)
    found: list[str] = []
    for name in sorted(pol.classes):
        other = pol.classes[name]
        if other.name == cls.name or not other.os_user:
            continue
        try:
            found += [r for r in class_roots(other, class_home(other, kind)) if not any(overlaps(r, o) for o in own)]
        except WsError:  # that class's own file says what is wrong with its repos
            continue
    return list(dict.fromkeys(found))


def class_settings(cls: policy.Class, home: str, kind: str) -> dict:
    """The managed-settings content for a class (see the module docstring). Raises WsError('usage') when the class's
    repos name no folder to allow writing in."""
    from ws.sandbox import class_domains  # late: ws.sandbox imports the adapters, which import this module

    roots = class_roots(cls, home)
    if not roots:
        raise WsError(
            "usage",
            f"class {cls.name!r} names no repo folder a settings file can hold "
            "(its repos are bare names or start with a wildcard)",
            f"edit `repos` under [classes.{cls.name}] in {policy.policy_path()} and give the class a folder, e.g. repos = "
            f'["/srv/work/**"]; then run `ws install --class {cls.name}` again',
            2,
        )
    return {
        "permissions": {BYPASS_KEY: BYPASS_OFF},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": [*roots, *scratch_dirs(kind)],
                "denyRead": ["~/"],
                "allowRead": list(roots),
            },
            # Default-deny network: only the class's hosts and its providers' (ws.sandbox.class_domains), and no other
            # settings file adds one. An egress exception is not in the file (it expires; the file does not).
            "network": {
                "allowManagedDomainsOnly": True,
                "allowedDomains": class_domains(cls),
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
        },
        "env": {INBOUND_ENV: INBOUND_VALUE},
    }


def class_file_text(cls: policy.Class, home: str, kind: str) -> str:
    return json.dumps(class_settings(cls, home, kind), indent=2) + "\n"


def launch_settings(
    cls: policy.Class,
    home: str,
    kind: str,
    pol: policy.Policy | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """The settings of a class's launch file (see the module docstring): what only a session started with `ws gate launch`
    gets. Only keys Claude Code reads from a `--settings` file, and none that would be wrong in a file every account reads
    (no `allowManaged*`). `pol`, when given, lets it seal the other classes' repos; `env` is the printing environment, for
    `sealed_paths`."""
    from ws.sandbox import class_domains  # late: ws.sandbox imports the adapters, which import this module

    sealed = sealed_paths(cls, home, kind, pol, env)
    return {
        "permissions": {
            READ_BLOCK_KEY: True,
            "deny": [*MESSAGING_DENY, *(f"{tool}({s})" for tool in ("Read", "Edit") for s in sealed)],
        },
        CROSS_KEY: CROSS_VALUE,
        PEER_KEY: True,
        HOOKS_KEY: False,
        "sandbox": {
            # the two switches that weaken the sandbox for a container or a MITM proxy: off, and a project may not set them
            **dict.fromkeys(WEAKER_KEYS, False),
            "filesystem": {"denyRead": _other_roots(cls, home, kind, pol)},
            # Default-deny network: the class's hosts and its providers' (ws.sandbox.class_domains); `strictAllowlist`
            # refuses any other host instead of asking. An egress exception is not in the file: it expires, the file does not.
            "network": {"strictAllowlist": True, "allowedDomains": class_domains(cls)},
        },
        "env": {WALL_MARKER: "1", **OTEL_OFF},
    }


def launch_file_text(
    cls: policy.Class,
    home: str,
    kind: str,
    pol: policy.Policy | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    return json.dumps(launch_settings(cls, home, kind, pol, env), indent=2) + "\n"


def removal_text(cls: policy.Class, kind: str) -> list[str]:
    """The lines that take a class's set-up out again, for `ws install --class` and `ws walls check`: both files, any
    half-placed copy of them, the launch folder, the class user and its home, and your own home folder's mode. Each command
    line pastes and runs in bash; a note sits on a `#` line of its own. Text only; ws runs none of it."""
    user = cls.os_user or ""
    q = shlex.quote
    managed, launch = class_file_path(cls.name, kind), launch_file_path(cls.name, kind)
    managed_tmp, launch_tmp = f"{managed}.tmp", f"{launch}.tmp"
    if kind == "macos":
        drop = [f"   sudo sysadminctl -deleteUser {q(user)}"]
    else:
        drop = ["   # removes the account and its home folder", f"   sudo userdel --remove {q(user)}"]
    return [
        "   # the managed-settings file, and a copy of it a stopped paste left",
        f"   sudo rm -f {q(str(managed))} {q(managed_tmp)}",
        "   # the launch file, a copy a stopped paste left, then the launch folder once no other class's file is in it",
        f"   sudo rm -f {q(str(launch))} {q(launch_tmp)}",
        f"   sudo rmdir {q(str(launch.parent))}",
        "   # the class account",
        *drop,
        "   # your own home folder: put it back to the mode it had before step 2 (755 is the usual one)",
        '   chmod 755 "$HOME"',
    ]


def install_text(cls: policy.Class, env: Mapping[str, str], kind: str, pol: policy.Policy | None = None) -> str:
    """What `ws install --class NAME` prints: the commands to run with sudo, the two root-owned files to place (the
    managed-settings file and the launch file) and how to check them. Text only; ws runs none of it and writes nothing."""
    user = cls.os_user or ""
    q = shlex.quote
    target, launch = class_file_path(cls.name, kind), launch_file_path(cls.name, kind)
    staged = f"{target}.tmp"  # not a `*.json`, so Claude Code does not read it while it is half written
    staged_launch = f"{launch}.tmp"
    group = "wheel" if kind == "macos" else "root"
    user_home = class_home(cls, kind)
    roots = class_roots(cls, user_home)
    mine = _home(env)
    under_mine = [r for r in roots if _within(r, mine)]
    create = (
        f"sudo sysadminctl -addUser {q(user)} -password -"
        if kind == "macos"
        else f"sudo useradd --create-home --shell /bin/bash {q(user)}"
    )
    body = class_file_text(cls, user_home, kind)
    launch_body = launch_file_text(cls, user_home, kind, pol, env)
    intro = f"Class {cls.name}: sessions run as the OS user {user}. ws creates no user and writes nothing; run these yourself."
    warning = (
        "A managed-settings file applies to every account on this machine, yours included, not only to "
        f"{user}. Read the file below before you place it. The launch file is read by `ws gate launch` from "
        f"{launch.parent}, beside Claude Code's managed-settings folder, which Claude Code does not read. A walled "
        "class is optional: it needs a second account on this Linux or macOS machine. To take it all out again, see step 8."
    )
    lines = [
        intro,
        "",
        warning,
        "",
        "1. Create the account (skip this if it exists) and close its home folder:",
        f"   {create}",
        f"   sudo chmod 700 {q(user_home)}",
        "2. Close your own home folder to other accounts:",
        '   chmod 700 "$HOME"',
        "3. Give the account the class's repos (a folder the account can reach: not one under your own home, which step 2",
        "   closes to it):",
        *[f"   sudo chown -R {q(user)} {q(r)}" for r in roots],
        *(
            [f"   Note: {', '.join(under_mine)} is under your own home ({mine}); use a folder outside it."]
            if under_mine
            else []
        ),
        "4. Place the managed-settings file and the launch file, each owned by root and writable by no one else. Each is",
        "   written under another name and moved into place, so a paste that stops half way leaves no half-written file",
        "   where it is read. First the managed-settings file:",
        f"   sudo mkdir -p {q(str(target.parent))}",
        f"   sudo tee {q(staged)} >/dev/null <<'WS_CLASS_FILE'",
        *body.rstrip("\n").splitlines(),
        "WS_CLASS_FILE",
        f"   sudo chown root:{group} {q(staged)}",
        f"   sudo chmod 644 {q(staged)}",
        f"   sudo mv {q(staged)} {q(str(target))}",
        "   Then the launch file, in a folder owned by root and writable by no one else:",
        f"   sudo mkdir -p {q(str(launch.parent))}",
        f"   sudo chown root:{group} {q(str(launch.parent))}",
        f"   sudo chmod 755 {q(str(launch.parent))}",
        f"   sudo tee {q(staged_launch)} >/dev/null <<'WS_LAUNCH_FILE'",
        *launch_body.rstrip("\n").splitlines(),
        "WS_LAUNCH_FILE",
        f"   sudo chown root:{group} {q(staged_launch)}",
        f"   sudo chmod 644 {q(staged_launch)}",
        f"   sudo mv {q(staged_launch)} {q(str(launch))}",
        "5. Give the account its own ws, policy and Claude Code. The ws under your home is out of its reach, and its",
        "   sessions read their own policy, so the class has to be in that one too:",
        f"   sudo -u {q(user)} -i",
        "   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash",
        "   ws setup",
        f"   (add the same [classes.{cls.name}] table to that account's policy file,",
        "   then run `ws gate generate --runner claude-code`)",
        "   claude   (sign in once as that account)",
        "6. Start the class's sessions as that account, from a repo of the class:",
        "   ws gate launch --runner claude-code -- claude",
        "7. Check it: `ws walls check` checks the launch file's settings are in place, and `ws doctor` reports each part",
        "   of the managed-settings file on its own line (WS014).",
        "8. To take it all out again:",
        *removal_text(cls, kind),
    ]
    return "\n".join(lines)


def _is_root(st: os.stat_result) -> bool:
    return st.st_uid == 0


def class_file_found(name: str) -> Path | None:
    """The class's file in a managed-settings folder, if one exists (a link counts: the row says what is wrong)."""
    for d in MANAGED_DIRS:
        p = Path(d) / class_file_name(name)
        if os.path.lexists(p):
            return p
    return None


def launch_file_found(name: str, kind: str) -> Path | None:
    """The class's launch file at `launch_file_path`, if one exists (a link counts: the check says what is wrong)."""
    p = launch_file_path(name, kind)
    return p if os.path.lexists(p) else None


def owner_problem(path: Path) -> tuple[str, str] | None:
    """(what is wrong, which fix) for a managed file that is not safe to rely on: a link, not root-owned, writable by
    group or others, not readable by the account that must obey it, or in a folder like that. The fix is one of `link`,
    `owner`, `mode`, `readable`, `folder` or `look`. None when the file is fine."""
    try:
        st = os.lstat(path)
        up = os.lstat(path.parent)
    except OSError as e:
        return f"can't be examined ({e.strerror})", "look"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "is a link or not a plain file", "link"
    if not _is_root(st):
        return "is not owned by root, so its owner can change the wall", "owner"
    if st.st_mode & 0o022:
        return "is writable by group or others", "mode"
    if not st.st_mode & 0o004:
        return (
            "can't be read by the class's account (no read for others), so its sessions run without the wall",
            "readable",
        )
    if not _is_root(up) or up.st_mode & 0o022:
        return "sits in a folder that is not root-owned or is writable by group or others", "folder"
    return None


def _owner_fix(path: Path, fix: str, again: str) -> str:
    """The command that clears the problem `owner_problem` named (`fix`), or the way to start again."""
    f, d = shlex.quote(str(path)), shlex.quote(str(path.parent))
    return {
        "link": f"sudo rm {f}, then place the file again: {again}",
        "owner": f"sudo chown root {f}",
        "mode": f"sudo chmod go-w {f}",
        "readable": f"sudo chmod 644 {f}",
        "folder": f"sudo chown root {d} && sudo chmod go-w {d}",
    }.get(fix, again)


def load_launch_file(cls: policy.Class, kind: str) -> dict:
    """The settings in a class's launch file, for the launch. Fails closed (WsError `refused`, exit 3, the hint naming
    `ws install --class NAME`): no file, a file that is a link, is not owned by root, is writable by group or others or
    sits in a folder like that, or is not a JSON object. The place is `launch_file_path`; nothing from the environment, a
    flag or a project file chooses it, and only this class's file is read."""
    path = launch_file_path(cls.name, kind)
    again = f"`ws install --class {cls.name}` prints the commands and the file"

    def refuse(why: str, hint: str) -> WsError:
        return WsError("refused", f"refusing to start: the launch file for {cls.name!r} {why}", hint, 3)

    if not os.path.lexists(path):
        raise refuse(f"is not in place ({path})", again)
    problem = owner_problem(path)
    if problem:
        raise refuse(f"({path}) {problem[0]}", _owner_fix(path, problem[1], again))
    doc = read(path)
    if doc is None:
        raise refuse(f"({path}) is not a JSON object", again)
    return doc


def _copy(value):
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    return [_copy(v) for v in value] if isinstance(value, list) else value


def merge(base: Mapping, class_doc: Mapping) -> dict:
    """`base` (the launch copy's settings) with a launch file merged in, the launch file's keys winning: a value it sets
    replaces the launch copy's, objects merge key by key, and lists are added to. The result is new; neither is changed."""
    out = _copy(base)
    for key, value in class_doc.items():
        have = out.get(key)
        if isinstance(value, Mapping) and isinstance(have, Mapping):
            out[key] = merge(have, value)
        elif isinstance(value, list) and isinstance(have, list):
            out[key] = [*have, *(_copy(x) for x in value if x not in have)]
        else:
            out[key] = _copy(value)
    return out


def read_denied(doc: Mapping) -> list[str]:
    """The `sandbox.filesystem.denyRead` entries of a launch file (the other classes' repo folders), as strings."""
    fs = (doc.get("sandbox") or {}).get("filesystem") or {}
    return [x for x in (fs.get("denyRead") or []) if isinstance(x, str)]


def narrowed(conf: Mapping, narrowing: policy.RepoSettings) -> dict:
    """`conf` with a per-repo table (policy.RepoSettings) applied: its `deny` rules added to `permissions.deny`, its
    `deny_read` and `deny_write` paths to the sandbox's. It only adds denials, so it can't widen what the launch file
    holds. Returns a new dict."""
    extra: dict = {}
    if narrowing.deny:
        extra["permissions"] = {"deny": list(narrowing.deny)}
    fs = {k: list(v) for k, v in (("denyRead", narrowing.deny_read), ("denyWrite", narrowing.deny_write)) if v}
    if fs:
        extra["sandbox"] = {"filesystem": fs}
    return merge(conf, extra)


# ---- what a project may hold in a walled session ----------------------------------------------------------------------

# An allow-list: a project's `.claude/settings*.json` may hold these top-level keys and no others, so a key Claude Code adds
# later is refused until it is named here. Names checked against the Claude Code settings reference (settings index and
# "Variables Claude Code ignores in env", https://code.claude.com/docs/en/settings-reference) and the environment variables
# reference (https://code.claude.com/docs/en/env-vars), 2026-11-12. `$schema` is the schema's own key.
PROJECT_KEYS = (
    "$schema",
    "model",
    "permissions",
    "attribution",
    "includeCoAuthoredBy",
    "outputStyle",
    "language",
    "cleanupPeriodDays",
    "respectGitignore",
    "sandbox",
    "env",
)
PROJECT_PERMISSION_KEYS = ("deny", "ask", "defaultMode")  # none of them allows a call
PROJECT_MODES = ("plan", "default")  # the other `defaultMode` values stop asking before a call
# `env` names a project may set, exact names only. A name outside it (a base URL, a proxy, a preload, a certificate
# bundle, a shell start-up file, a prefix such as WS_ or OTEL_) is refused: any of them can redirect traffic or run code.
PROJECT_ENV = (
    "NODE_ENV",
    "TZ",
    "LANG",
    "LC_ALL",
    "DISABLE_TELEMETRY",
    "DISABLE_AUTOUPDATER",
    "BASH_DEFAULT_TIMEOUT_MS",
    "BASH_MAX_TIMEOUT_MS",
    "MAX_THINKING_TOKENS",
)
_NOT_ALLOWED = "is not a setting a walled session lets a project change"
# A project's `sandbox` table may only narrow. These are the sub-keys that can add a denial, each judged by value: a switch
# only in the direction that keeps the wall (`enabled` true, `allowUnsandboxedCommands` false, ...), a list only of plain
# names, and `credentials` only in `deny` mode. Every other sub-key is refused, whatever its value: `excludedCommands`,
# `ignoreViolations`, `allowRead`, `allowWrite`, `allowedDomains`, a proxy port, a socket, `enableWeaker*` and the keys
# Claude Code adds later. Names checked against https://code.claude.com/docs/en/sandboxing, 2026-11-12.
SANDBOX_SWITCHES = {
    "enabled": True,
    "failIfUnavailable": True,
    "allowUnsandboxedCommands": False,
    "autoAllowBashIfSandboxed": False,
}
SANDBOX_DENY_LISTS = {"filesystem": ("denyRead", "denyWrite"), "network": ("deniedDomains",)}
SANDBOX_CREDENTIALS = {"files": ("path", "mode"), "envVars": ("name", "mode")}
_WIDER = "is a sandbox setting a walled session lets a project change only to narrow it"


def _sandbox_adds(sb: object) -> tuple[str, str] | None:
    """(the setting, why it is refused) for the first thing in a project's `sandbox` table that could widen the wall, or
    None."""
    if not isinstance(sb, dict):
        return "sandbox", "is not a table"
    for key, value in sb.items():
        at = f"sandbox.{key}"
        if key in SANDBOX_SWITCHES:
            if value is not SANDBOX_SWITCHES[key]:
                return at, "opens the sandbox the class requires" if key == "enabled" else _WIDER
        elif key in SANDBOX_DENY_LISTS:
            if not isinstance(value, dict):
                return at, "is not a table"
            for sub, names in value.items():
                if sub not in SANDBOX_DENY_LISTS[key]:
                    return f"{at}.{sub}", _WIDER
                if not (isinstance(names, list) and all(isinstance(n, str) and n for n in names)):
                    return f"{at}.{sub}", "is not a list of names"
        elif key == "credentials":
            if not isinstance(value, dict):
                return at, "is not a table"
            for sub, entries in value.items():
                allowed = SANDBOX_CREDENTIALS.get(sub)
                if allowed is None:
                    return f"{at}.{sub}", _WIDER
                if not (
                    isinstance(entries, list)
                    and all(isinstance(e, dict) and e.get("mode") == "deny" and set(e) <= set(allowed) for e in entries)
                ):
                    return f"{at}.{sub}", "is not a list of entries in deny mode"
        else:
            return at, _WIDER
    return None


def _adds(rel: str, conf: object) -> tuple[str, str] | None:
    """(the setting, why it is refused) for the first thing a project file holds that a walled session does not allow, or
    None. `.mcp.json` may start no MCP server. A settings file may hold only `PROJECT_KEYS`; within them `permissions` holds
    only `deny`, `ask` and a `defaultMode` of `PROJECT_MODES`, `env` only the `PROJECT_ENV` names, and `sandbox` only what
    `_sandbox_adds` allows. The one rule: nothing else is listed, so nothing else needs a deny."""
    if not isinstance(conf, dict):
        return None
    if rel == LOAD_FILES[0]:
        servers = conf.get("mcpServers")
        if isinstance(servers, dict) and servers:
            return f"mcpServers.{next(iter(servers))}", "starts an MCP server the launch file does not list"
        return None
    for key, value in conf.items():
        if key not in PROJECT_KEYS:
            return str(key), _NOT_ALLOWED
        if key == "permissions":
            if not isinstance(value, dict):
                return key, "is not a table of rules"
            for sub, rule in value.items():
                if sub not in PROJECT_PERMISSION_KEYS:
                    return f"permissions.{sub}", _NOT_ALLOWED
                if sub == "defaultMode" and rule not in PROJECT_MODES:
                    return "permissions.defaultMode", "stops asking before a call"
        elif key == "env":
            if not isinstance(value, dict):
                return key, "is not a table of names"
            for name in value:
                if name not in PROJECT_ENV:
                    return f"env.{name}", "is an environment name a walled session does not let a project set"
        elif key == "sandbox":
            found = _sandbox_adds(value)
            if found:
                return found
    return None


# Skills, agents and commands carry YAML frontmatter, and some of its keys run code or allow calls on their own: a `hooks`
# table runs a command on an event, an agent's `mcpServers` starts a server, its `permissionMode` can stop the asking, and
# `allowed-tools` allows a tool with no question. Each tree is (folder under `.claude`, the file names it loads, the keys
# refused there); a key is compared with case, `-` and `_` left out, so `Allowed_Tools` is `allowed-tools`.
FRONTMATTER_TREES = (
    ("skills", lambda name: name.lower() == "skill.md", ("hooks", "allowed-tools")),
    ("agents", lambda name: name.lower().endswith(".md"), ("hooks", "mcpServers", "permissionMode")),
    ("commands", lambda name: name.lower().endswith(".md"), ("hooks", "allowed-tools")),
)
WORKFLOWS_FOLDER = "workflows"  # `.claude/workflows/*.js`: a script Claude Code runs; any of them is refused
_FRONT_WHY = {
    "hooks": "runs a command of its own on an event, outside the launch file's hooks",
    "mcpservers": "starts an MCP server the launch file does not list",
    "permissionmode": "can stop the asking before a call",
    "allowedtools": "allows a tool call without asking",
}
_FRONT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*:(?:[ \t]|$)")
_MAX_FILES = 2000  # entries looked at in one project folder: more is refused, not skipped
_MAX_DEPTH = 12


def _flat(key: str) -> str:
    return re.sub(r"[-_]", "", key).lower()


def frontmatter_keys(text: str) -> list[str]:
    """The top-level keys of a file's YAML frontmatter (the block between a first `---` line and the next), [] when the file
    has none. Small and strict on purpose: it reads key lines and skips the indented or `- ` lines under a key, and raises
    ValueError with the reason for anything else (no closing fence, a CR, a quoted or odd key, an indented line with no key
    above it, a fence that is not on the first line), so a header ws cannot read is a refusal and never a skip."""
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None or lines[first].strip() != "---":
        return []
    if first != 0:
        raise ValueError("the frontmatter does not start on the first line")
    if lines[0] != "---":
        raise ValueError("the first fence is not a bare `---` line (a CR or a space after it)")
    keys: list[str] = []
    for n, line in enumerate(lines[1:], 2):
        if "\r" in line:
            raise ValueError(f"line {n} has a carriage return (CRLF line ends)")
        if line == "---":
            return keys
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" or line == "-" or line.startswith("- "):
            if not keys:
                raise ValueError(f"line {n} is indented under no key")
            continue
        if line[0] in "\"'":
            raise ValueError(f"line {n} has a quoted key")
        if not _FRONT_KEY.match(line):
            raise ValueError(f"line {n} is not a plain `key: value` line")
        keys.append(line.split(":", 1)[0])
    raise ValueError("there is no closing `---` line")


def _unreadable(f: Path, why: str, hint: str) -> WsError:
    return WsError("project_settings_unreadable", f"{f} {why}; refused to be safe", hint, 3, {"file": str(f)})


def _read_text(f: Path) -> str:
    """The file's text as the runner reads it (UTF-8, a byte order mark dropped), line ends untouched. Opened without
    following a link and without blocking, and refused unless it is a regular file of at most `_MAX_BYTES`: ApprovalError."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(f, flags)
    except OSError as e:
        raise ApprovalError(f"unreadable ({type(e).__name__})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ApprovalError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ApprovalError("too large")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ApprovalError(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(fd)


def _tree(top: Path, wanted, budget: list[int]) -> list[Path]:
    """The files under `top` whose name `wanted` accepts, links never followed. ApprovalError for a link that is a folder or
    would be wanted, for a folder it cannot list and for more entries than `_MAX_FILES` (`budget` counts them across trees)."""
    out: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise ApprovalError("folders nested too deep")
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            raise ApprovalError(f"unreadable ({type(e).__name__})") from e
        for e in entries:
            budget[0] += 1
            if budget[0] > _MAX_FILES:
                raise ApprovalError("too many files")
            if e.is_symlink():
                if e.is_dir() or wanted(e.name):
                    raise ApprovalError(f"{e.path} is a link")
            elif e.is_dir(follow_symlinks=False):
                walk(Path(e.path), depth + 1)
            elif wanted(e.name):
                out.append(Path(e.path))

    walk(top, 0)
    return out


def _main_checkout(d: Path) -> Path | None:
    """The main checkout of the git worktree whose root is `d`, else None: `d/.git` is a file naming the worktree's folder,
    whose `commondir` names the repository's `.git`, and the checkout is that folder's parent. Claude Code reads a
    worktree's settings from the main checkout too. ApprovalError for a `.git` file or a `commondir` it cannot read."""
    git = d / ".git"
    try:
        if not stat.S_ISREG(git.lstat().st_mode):
            return None
    except OSError:
        return None
    line = next((x for x in read_regular(git, 4096).splitlines() if x.startswith("gitdir:")), None)
    if line is None:
        return None
    gitdir = Path(os.path.normpath(d / line.split(":", 1)[1].strip()))
    if not os.path.lexists(gitdir / "commondir"):
        return None
    common = Path(os.path.normpath(gitdir / read_regular(gitdir / "commondir", 4096).strip()))
    return common.parent if common.name == ".git" else None


def _folders(starts: Iterable[str], home: Path) -> list[Path]:
    """The folders whose project files a walled session loads: each start and every folder above it (the home folder's own
    excluded), and the main checkout of each worktree among them."""
    seen: dict[Path, None] = {}
    for start in starts:
        if not isinstance(start, str) or not start.startswith("/"):
            continue
        for d in (Path(start), *Path(start).parents):
            if d == home or d in seen:
                continue
            seen[d] = None
            main = _main_checkout(d)
            if main is not None and main != home:
                seen.setdefault(main)
    return list(seen)


def _refused(f: Path, key: str, why: str, delete: str) -> WsError:
    return WsError(
        "project_load_refused",
        f"{f} holds {key}, which {why}, and a walled session does not allow it",
        f"delete {delete}, or work on the project outside the class",
        3,
        {"file": str(f), "key": key},
    )


def _json_problem(d: Path) -> WsError | None:
    """The refusal for what a folder's `.mcp.json` and settings files hold or how they read, or None."""
    for rel in LOAD_FILES:
        f = d / rel
        if not os.path.lexists(f):
            continue
        rm = f"`rm {shlex.quote(str(f))}`"
        try:
            conf = json.loads(_read_text(f))
        except ApprovalError as e:
            return _unreadable(
                f,
                f"is not a plain readable file ({e})",
                f"make it a plain JSON file under 1 MB, or delete it with {rm}",
            )
        except (ValueError, RecursionError) as e:
            return _unreadable(
                f, f"does not parse as JSON ({type(e).__name__})", f"make it a JSON object, or delete it with {rm}"
            )
        if not isinstance(conf, dict):
            return _unreadable(f, "is not a JSON object", f"make it a JSON object, or delete it with {rm}")
        if not conf and rel != LOAD_FILES[0]:
            return _unreadable(f, "is an empty object", f"put your settings in it, or delete it with {rm}")
        found = _adds(rel, conf)
        if found:
            key, why = found
            return _refused(f, key, why, rm if rel == LOAD_FILES[0] else f"`{key}` from {f}")
    return None


def _doc_problem(d: Path) -> WsError | None:
    """The refusal for what a folder's skills, agents, commands and workflows hold or how they read, or None."""
    claude = d / ".claude"
    budget = [0]
    for folder, wanted, refused in (*FRONTMATTER_TREES, (WORKFLOWS_FOLDER, lambda n: n.lower().endswith(".js"), ())):
        top = claude / folder
        if not os.path.lexists(top):
            continue
        try:
            if top.is_symlink():
                raise ApprovalError("it is a link")
            files = _tree(top, wanted, budget) if top.is_dir() else []
        except ApprovalError as e:
            hint = f"make it a plain folder of plain files, or delete it with `rm -r {shlex.quote(str(top))}`"
            return _unreadable(top, f"cannot be read in full ({e})", hint)
        for f in files:
            rm = f"`rm {shlex.quote(str(f))}`"
            if folder == WORKFLOWS_FOLDER:
                return _refused(f, "a workflow script", "Claude Code runs as code of its own", rm)
            try:
                keys = frontmatter_keys(_read_text(f))
            except ApprovalError as e:
                return _unreadable(
                    f, f"is not a plain readable file ({e})", f"make it a plain text file, or delete it with {rm}"
                )
            except ValueError as e:
                hint = f"write the header as plain `key: value` lines between two `---` lines, or delete the file with {rm}"
                return _unreadable(f, f"has frontmatter ws cannot read ({e})", hint)
            bad = {_flat(k) for k in refused}
            for key in keys:
                if _flat(key) in bad:
                    return _refused(f, key, _FRONT_WHY[_flat(key)], f"`{key}` from the frontmatter of {f}")
    return None


def project_problem(starts: Iterable[str], home: Path) -> WsError | None:
    """The refusal for a walled session whose project holds anything a walled session does not allow, or None. It reads, in
    each start folder, each folder above it (the home folder's own excluded) and the main checkout of a git worktree (Claude
    Code reads the project's from where the session began, whatever it changes to): `.mcp.json`, `.claude/settings.json` and
    `settings.local.json` against `_adds`; the frontmatter of `.claude/skills/**/SKILL.md`, `.claude/agents/**/*.md` and
    `.claude/commands/**/*.md` against `FRONTMATTER_TREES` (`frontmatter_keys` is the reader); and `.claude/workflows/*.js`,
    any of which is refused. A kind `project_load_refused` (exit 3) error whose hint names the file and what to delete.
    Fail closed (kind `project_settings_unreadable`, exit 3): a file that is a link, a FIFO or too big, one that exists but
    is empty, is not a JSON object or does not parse (a BOM is read as the runner reads it), a frontmatter ws cannot read in
    full, a `.claude` or a folder in it that is a link or cannot be listed. An absent file is fine. The one rule `ws gate
    launch` runs before the runner starts (a SessionStart hook cannot block) and the hook runs on every call
    (claude_code.preflight_refusal). A skill's `!command` body is not read, so it stays allowed (docs/explanation/known-limits.md)."""
    try:
        folders = _folders(starts, home)
    except ApprovalError as e:
        return WsError(
            "project_settings_unreadable",
            f"a .git file or its worktree link cannot be read ({e}); refused to be safe",
            "make the `.git` file a plain file naming its folder, or work on the project outside the class",
            3,
        )
    for d in folders:
        claude = d / ".claude"
        if claude.is_symlink():
            hint = f"replace it with a real folder, or delete the link with `rm {shlex.quote(str(claude))}`"
            return _unreadable(claude, "is a link, not a folder", hint)
        problem = _json_problem(d) or _doc_problem(d)
        if problem:
            return problem
    return None


def class_files(pol: policy.Policy, env: Mapping[str, str], kind: str) -> list[tuple[policy.Class, Path, dict]]:
    """(class, its file in place, the settings ws renders for it) for each class with an OS user whose file exists. A
    class whose repos name no folder has nothing to compare with and is left out."""
    out = []
    for cls in class_users(pol):
        found = class_file_found(cls.name)
        if found is None:
            continue
        try:
            want = class_settings(cls, class_home(cls, kind), kind)
        except WsError:
            continue
        out.append((cls, found, want))
    return out


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def stale_networks(pol: policy.Policy, env: Mapping[str, str], kind: str) -> list[str]:
    """The classes (by name) whose managed-settings file in place holds a `network` block other than the one ws renders
    now: a host added to or taken out of the policy since the file was placed. A class with no file in place is left out
    (WS014 says so)."""
    out = []
    for cls, found, want in class_files(pol, env, kind):
        doc = read(found)
        sb = doc.get("sandbox") if isinstance(doc, dict) else None
        have = sb.get("network") if isinstance(sb, dict) else None
        if have != want["sandbox"]["network"]:
            out.append(cls.name)
    return out


def class_widenings(doc: Mapping, file: Path, want: Mapping, home: str) -> list[Widening]:
    """The settings in `doc` (your own file) that go beyond a class file in place: a place to write or read the file does
    not name, or a bypass setting that is not the class file's. The generic switches (the sandbox off, an excluded
    command) are `widenings`' to find."""
    found: list[Widening] = []

    def add(path: tuple[str, ...], item: str, why: str) -> None:
        found.append(Widening(path, item, file, why))

    sb = doc.get("sandbox")
    fs_have = sb.get("filesystem") if isinstance(sb, Mapping) else None
    fs_want = want["sandbox"]["filesystem"]
    for key, why in (
        ("allowWrite", "lets a session write where the class file does not (it names the repos and scratch only)"),
        ("allowRead", "re-opens a folder the class file hides (the home folder, but for the repos)"),
    ):
        entries = fs_have.get(key) if isinstance(fs_have, Mapping) else None
        allowed = [p for p in (_norm(x, home) for x in fs_want.get(key, [])) if p]
        for e in entries if isinstance(entries, list) else ([entries] if entries else []):
            p = _norm(e, home)
            if p is None or not any(_within(p, a) for a in allowed):
                add(("sandbox", "filesystem", key), str(e), why)
    perms = doc.get("permissions")
    if isinstance(perms, Mapping) and BYPASS_KEY in perms and perms[BYPASS_KEY] != BYPASS_OFF:
        add(
            ("permissions", BYPASS_KEY),
            json.dumps(perms[BYPASS_KEY]),
            "is not the class file's value, so bypass may be on",
        )
    return found


def class_rows(pol: policy.Policy, env: Mapping[str, str], kind: str) -> list[Row]:
    """One row per class with an OS user per part (PARTS), each judged on its own. The identity and the file come from the
    system (the user database, the file's owner and mode), never from the environment. A missing user or file is a
    `warn` that says which part, a file that is there and wrong is a `fail`."""
    rows: list[Row] = []
    for cls in class_users(pol):
        rows += _rows_for(cls, env, kind)
    return rows


def _rows_for(cls: policy.Class, env: Mapping[str, str], kind: str) -> list[Row]:
    name = cls.name
    again = f"`ws install --class {name}` prints the commands and the file"

    def row(part: str, status: str, detail: str, fix: str = "") -> Row:
        return Row(name, part, status, detail, fix)

    rows: list[Row] = []
    if passwd(cls.os_user or "") is None:
        rows.append(row(PARTS[0], WARN, "the class's OS user is not in this system's user database", again))
    else:
        rows.append(row(PARTS[0], OK, "the class's OS user exists"))
    found = class_file_found(name)
    if found is None:
        where = class_file_path(name, kind)
        rows.append(row(PARTS[1], WARN, f"no managed-settings file for the class at {where}", again))
        rows += [row(p, WARN, "not checked: no class file in place", again) for p in PARTS[2:7]]
    else:
        problem = owner_problem(found)
        if problem:
            rows.append(row(PARTS[1], FAIL, f"{found} {problem[0]}", _owner_fix(found, problem[1], again)))
        else:
            rows.append(row(PARTS[1], OK, f"{found} is root-owned and writable by no one else"))
        doc = read(found)
        if doc is None:
            rows += [row(p, FAIL, f"{found} is not a JSON object Claude Code can read", again) for p in PARTS[2:7]]
        else:
            try:
                want = class_settings(cls, class_home(cls, kind), kind)
            except WsError as e:
                rows += [row(p, WARN, e.message, e.hint) for p in PARTS[2:7]]
            else:
                rows += _content_rows(name, doc, want, again)
    rows.append(_inbound_row(cls, found))
    return rows


def _content_rows(name: str, doc: Mapping, want: Mapping, again: str) -> list[Row]:
    sb = doc.get("sandbox") if isinstance(doc.get("sandbox"), Mapping) else {}
    fs = sb.get("filesystem") if isinstance(sb.get("filesystem"), Mapping) else {}
    perms = doc.get("permissions") if isinstance(doc.get("permissions"), Mapping) else {}
    expect = want["sandbox"]["filesystem"]

    def entries(key: str) -> list:
        return fs.get(key) if isinstance(fs.get(key), list) else []

    def row(part: str, good: bool, ok: str, bad: str) -> Row:
        return Row(name, part, OK if good else FAIL, ok if good else bad, "" if good else again)

    extra_w = [str(x) for x in entries("allowWrite") if x not in expect["allowWrite"]]
    lacks_w = [x for x in expect["allowWrite"] if x not in entries("allowWrite")]
    extra_r = [str(x) for x in entries("allowRead") if x not in expect["allowRead"]]
    hidden = "~/" in entries("denyRead")
    return [
        row(
            PARTS[2],
            sb.get("enabled") is True and sb.get("failIfUnavailable") is True,
            "the sandbox is on and Claude Code refuses to start without it",
            "the file does not set sandbox.enabled and sandbox.failIfUnavailable to true",
        ),
        row(
            PARTS[3],
            sb.get("allowUnsandboxedCommands") is False,
            "a command the sandbox stops cannot be run again outside it",
            "the file does not set sandbox.allowUnsandboxedCommands to false",
        ),
        row(
            PARTS[4],
            perms.get(BYPASS_KEY) == BYPASS_OFF,
            f"permissions.{BYPASS_KEY} is {BYPASS_OFF!r}",
            f"the file does not set permissions.{BYPASS_KEY} to {BYPASS_OFF!r}",
        ),
        row(
            PARTS[5],
            not extra_w and not lacks_w,
            "writes are limited to the class's repos and scratch",
            f"allowWrite is wider than the class's repos and scratch: {', '.join(extra_w)}"
            if extra_w
            else f"allowWrite lacks {', '.join(lacks_w)}",
        ),
        row(
            PARTS[6],
            hidden and not extra_r,
            "the home folder is unreadable except the class's repos",
            "denyRead does not hide the home folder (~/)"
            if not hidden
            else f"allowRead re-opens more than the class's repos: {', '.join(extra_r)}",
        ),
    ]


def _inbound_row(cls: policy.Class, found: Path | None) -> Row:
    """The inbound part: the file records lead-only, and the policy's own `inbound` for the class lets no one else in."""
    part, again = PARTS[7], f"`ws install --class {cls.name}` prints the file"
    if cls.inbound == "allowlist":
        fix = f'set inbound = "lead-only" under [classes.{cls.name}] in the policy'
        return Row(cls.name, part, WARN, "the policy lets named sessions send in (inbound = allowlist)", fix)
    if found is None:
        return Row(cls.name, part, WARN, "not checked in the file: no class file in place", again)
    doc = read(found)
    env = doc.get("env") if isinstance(doc, dict) and isinstance(doc.get("env"), Mapping) else {}
    if env.get(INBOUND_ENV) != INBOUND_VALUE:
        return Row(cls.name, part, FAIL, f"the file does not set env {INBOUND_ENV} to {INBOUND_VALUE!r}", again)
    return Row(cls.name, part, OK, f"inbound is {cls.inbound} in the policy and recorded as lead-only in the file")
