"""What the sandbox settings Claude Code reads say about the wall: the keys that widen it, and `ws doctor`'s WS012.

In Claude Code the `sandbox` block of the settings is the wall for shell commands. ws writes the block, and Claude Code
merges it with every other settings file it reads, so a setting in your own file or in a managed-settings file can open
it again. This module finds those settings. `ws install` refuses to merge beside them unless you keep them on purpose
(`--allow-widened-sandbox`), and WS012 reports them, with a stale `denyRead` list, in place of "ok".

It reads files; it proves nothing about the host. A settings source ws cannot see (a `--settings` flag, a managed policy
held outside the files below) can still widen the wall, and WS012 says only what it can read.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
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


def scan(settings: Path, block: Mapping, env: Mapping[str, str], *, managed: bool = True) -> list[Widening]:
    """The widening settings in your settings file and, with `managed`, in every managed-settings file ws can read."""
    home = _home(env)
    found: list[Widening] = []
    for f in [settings, *(managed_files() if managed else [])]:
        doc = _read(f)
        if doc is not None:
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
        return WARN, f"unsandboxed: {where} has no sandbox block; {STRICT}; {FOR_FEWER}", "run `ws install`"
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
    found = scan(where, block, env)
    if found:
        problems.append("wall widened by user: " + "; ".join(f"{w.label} ({w.file})" for w in found))
        fix = fix or (
            "take those settings out (then `ws install` if you used " + OVERRIDE + "), or accept the wider wall"
        )
    if problems:
        return WARN, "; ".join(problems), fix
    return OK, f"the sandbox block is in {where} and covers every deny_read path the wall can hide", ""
