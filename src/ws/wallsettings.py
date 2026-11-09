"""What the sandbox settings Claude Code reads say about the wall: the keys that widen it, and `ws doctor`'s WS012.

In Claude Code the `sandbox` block of the settings is the wall for shell commands. ws writes the block, and Claude Code
merges it with every other settings file it reads, so a setting in your own file or in a managed-settings file can open
it again. This module finds those settings. `ws install` refuses to merge beside them unless you keep them on purpose
(`--allow-widened-sandbox`), and WS012 reports them, with a stale `denyRead` list, in place of "ok".

It reads files; it proves nothing about the host. A settings source ws cannot see (a `--settings` flag, a managed policy
held outside the files below) can still widen the wall, and WS012 says only what it can read.

A class with an `os_user` (see ws.policy.Class) can also have a managed-settings file of its own, which holds what a
session of that class may do whoever starts it: the sandbox required, no unsandboxed retry, bypass off, writes in the
class's repos and scratch only, the home folder unreadable except those repos, inbound lead-only. This module renders
that file as text (`class_settings`, `install_text`, for `ws install --class`) and judges it (`class_rows`, the WS014
rows). It never writes it: a managed-settings file is placed by a person with sudo, and it applies to every account on
the machine, so ws prints it and nothing more. `MANAGED_FILES` and `MANAGED_DIRS` are the only places managed-settings
paths come from.

Python 3.11+, standard library only (`pwd` is imported inside the functions that need it).
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ws import policy
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
INBOUND_ENV = "WS_INBOUND"  # the class file's record of who may send in
INBOUND_VALUE = "lead-only"
CLASS_FILE_PREFIX = "50-ws-class-"

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


def _read(path: Path) -> dict | None:
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


def _overlaps(a: str, b: str) -> bool:
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
    `allowRead` or `allowWrite` entry that isn't a path ws can resolve counts as one."""
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
                elif any(_overlaps(p, h) for h in held):
                    add(
                        ("sandbox", "filesystem", key),
                        str(e),
                        "re-opens a path the wall hides or keeps read-only (a deny_read path, or one of ws's own)",
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
        doc = _read(f)
        if doc is not None and not (own and own.get(f) == doc):
            found += widenings(doc, f, block, home)
    return found


def refusal(found: list[Widening], where: Path) -> WsError:
    """The refusal `ws install` gives when your settings widen the wall: each setting, its file and what it does."""
    lines = "".join(f"\n  {w.label} ({w.file}): {w.why}" for w in found)
    return WsError(
        "refused",
        f"{where}: these settings widen the sandbox ws relies on, so ws changed nothing:{lines}",
        f"take them out and run `ws install` again; or keep them on purpose with `ws install {OVERRIDE}` "
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


# ---- a class with an OS user: its managed-settings file ----------------------------------------------------------------

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


def _passwd(name: str):
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


def class_file_name(name: str) -> str:
    return f"{CLASS_FILE_PREFIX}{name}.json"


def class_dir(kind: str) -> str:
    """The managed-settings folder a class file goes in on this system: the macOS one there, the first one elsewhere."""
    if kind == "macos" and len(MANAGED_DIRS) > 1:
        return MANAGED_DIRS[-1]
    return MANAGED_DIRS[0]


def class_file_path(name: str, kind: str) -> Path:
    return Path(class_dir(kind)) / class_file_name(name)


def scratch_dirs(kind: str) -> tuple[str, ...]:
    return ("/tmp", "/private/tmp") if kind == "macos" else ("/tmp",)


def class_home(cls: policy.Class, kind: str) -> str:
    """The home folder a `~` in the class's repos means, and the one printed step 1 closes: the class user's own when the
    system knows the account, else the folder the printed `useradd` or `sysadminctl` makes for it (`/home/NAME`,
    `/Users/NAME`). Never the home of whoever runs ws: the file and the `chown` must name the same folders before and
    after the account exists."""
    user = cls.os_user or ""
    entry = _passwd(user)
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
                'name each repo folder, or the folder that holds them as "/path/**"',
                2,
            )
        root = _norm(text, home)
        if root and root != "/":
            roots.append(root)
    return list(dict.fromkeys(roots))


def class_settings(cls: policy.Class, home: str, kind: str) -> dict:
    """The managed-settings content for a class (see the module docstring). Raises WsError('usage') when the class's
    repos name no folder to allow writing in."""
    roots = class_roots(cls, home)
    if not roots:
        raise WsError(
            "usage",
            f"class {cls.name!r} names no repo folder a settings file can hold "
            "(its repos are bare names or start with a wildcard)",
            'give the class a repo as a folder, e.g. repos = ["/srv/work/**"]',
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
        },
        "env": {INBOUND_ENV: INBOUND_VALUE},
    }


def class_file_text(cls: policy.Class, home: str, kind: str) -> str:
    return json.dumps(class_settings(cls, home, kind), indent=2) + "\n"


def install_text(cls: policy.Class, env: Mapping[str, str], kind: str) -> str:
    """What `ws install --class NAME` prints: the commands to run with sudo, the root-owned file to place and how to check
    it. Text only; ws runs none of it and writes nothing."""
    user = cls.os_user or ""
    q = shlex.quote
    target = class_file_path(cls.name, kind)
    staged = f"{target}.tmp"  # not a `*.json`, so Claude Code does not read it while it is half written
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
    intro = f"Class {cls.name}: sessions run as the OS user {user}. ws creates no user and writes nothing; run these yourself."
    warning = (
        "A managed-settings file applies to every account on this machine, yours included, not only to "
        f"{user}. Read the file below before you place it. To take it out again: sudo rm {q(str(target))}"
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
        "4. Place the managed-settings file, owned by root and writable by no one else. It is written under another name and",
        "   moved into place, so a paste that stops half way leaves no half-written file where Claude Code reads it:",
        f"   sudo mkdir -p {q(str(target.parent))}",
        f"   sudo tee {q(staged)} >/dev/null <<'WS_CLASS_FILE'",
        *body.rstrip("\n").splitlines(),
        "WS_CLASS_FILE",
        f"   sudo chown root:{group} {q(staged)}",
        f"   sudo chmod 644 {q(staged)}",
        f"   sudo mv {q(staged)} {q(str(target))}",
        "5. Give the account its own ws, policy and Claude Code. The ws under your home is out of its reach, and its",
        "   sessions read their own policy, so the class has to be in that one too:",
        f"   sudo -u {q(user)} -i",
        "   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash",
        "   ws setup",
        f"   (add the same [classes.{cls.name}] table to that account's policy.toml,",
        "   then run `ws gate generate --runner claude-code`)",
        "   claude   (sign in once as that account)",
        "6. Start the class's sessions as that account, from a repo of the class:",
        "   ws gate launch --runner claude-code -- claude",
        "7. Check it: `ws doctor` reports each part of the class on its own line (WS014).",
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


def _owner_problem(path: Path) -> tuple[str, str] | None:
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
    """The command that clears the problem `_owner_problem` named (`fix`), or the way to start again."""
    f, d = shlex.quote(str(path)), shlex.quote(str(path.parent))
    return {
        "link": f"sudo rm {f}, then place the file again: {again}",
        "owner": f"sudo chown root {f}",
        "mode": f"sudo chmod go-w {f}",
        "readable": f"sudo chmod 644 {f}",
        "folder": f"sudo chown root {d} && sudo chmod go-w {d}",
    }.get(fix, again)


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
    if _passwd(cls.os_user or "") is None:
        rows.append(row(PARTS[0], WARN, "the class's OS user is not in this system's user database", again))
    else:
        rows.append(row(PARTS[0], OK, "the class's OS user exists"))
    found = class_file_found(name)
    if found is None:
        where = class_file_path(name, kind)
        rows.append(row(PARTS[1], WARN, f"no managed-settings file for the class at {where}", again))
        rows += [row(p, WARN, "not checked: no class file in place", again) for p in PARTS[2:7]]
    else:
        problem = _owner_problem(found)
        if problem:
            rows.append(row(PARTS[1], FAIL, f"{found} {problem[0]}", _owner_fix(found, problem[1], again)))
        else:
            rows.append(row(PARTS[1], OK, f"{found} is root-owned and writable by no one else"))
        doc = _read(found)
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
    doc = _read(found)
    env = doc.get("env") if isinstance(doc, dict) and isinstance(doc.get("env"), Mapping) else {}
    if env.get(INBOUND_ENV) != INBOUND_VALUE:
        return Row(cls.name, part, FAIL, f"the file does not set env {INBOUND_ENV} to {INBOUND_VALUE!r}", again)
    return Row(cls.name, part, OK, f"inbound is {cls.inbound} in the policy and recorded as lead-only in the file")
