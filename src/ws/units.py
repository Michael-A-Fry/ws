"""`ws unit`: the one way a systemd user unit reaches `~/.config/systemd/user`, and the gate's rules about running one (C867).

A session that can write a user unit and start it runs code outside every PreToolUse hook (the manager starts it, not the
session). So the gate does three things, all here:

  * the unit folders (`~/.config/systemd/user`, `~/.config/environment.d`, `~/.config/autostart`, and the two other folders
    systemd reads user units from) are protected paths: a session's own write to them is refused, and the wall makes them
    read-only (`paths.unit_dirs`);
  * `ws unit install NAME` is the way in. It runs as a program, not as command text, and it copies a unit (and its
    drop-ins) only from a folder the policy names in `[gate] unit_sources`: a folder in a checkout where the change was
    committed and joined, never a session's own worktree. The source file has to be tracked by git and unchanged;
  * `systemctl --user enable|start|restart|...` passes only for a unit whose file, and every file of its `.d` folder, is
    byte for byte the committed source. A unit that exists in a user-writable unit folder and is not that is refused.
    A unit that exists in none of them is a system unit, or does not exist, and passes.

Also refused: `systemctl --user link|edit|add-wants|add-requires|set-environment|import-environment|unset-environment`,
`loginctl enable-linger`, and the `systemd-run` options that leave something behind (`--on-*`, `--timer-property`,
`--remain-after-exit`, `--no-block` without `--wait`). Transient `systemd-run --scope` and `--wait` runs are allowed.

What this cannot see: a program that writes a unit from inside its own code. The wall closes that on Linux (the folders are
read-only); a separate Unix user for agents closes it everywhere. `ws unit install` run by a session inside the wall
fails for the same reason, which is the point: units are installed from the person's own session.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
from pathlib import Path

from ws import output, paths
from ws.output import WsError

COMMAND = "unit"

USER_UNIT_DIRS = (".config/systemd/user", ".config/systemd/user.control", ".local/share/systemd/user")
OTHER_PROTECTED = (".config/environment.d", ".config/autostart")

START_VERBS = frozenset(
    {
        "enable",
        "reenable",
        "start",
        "restart",
        "try-restart",
        "reload-or-restart",
        "try-reload-or-restart",
        "reload",
        "isolate",
    }
)
NEVER_VERBS = frozenset(
    {"link", "edit", "add-wants", "add-requires", "set-environment", "import-environment", "unset-environment"}
)
SYSTEMCTL_VALUED = frozenset(
    {"-M", "-H", "-p", "-t", "-s", "-n", "-o", "-T", "--host", "--machine", "--property", "--type"}
)
RUN_PERSISTS = ("--on-", "--timer-property", "--remain-after-exit")


def unit_dirs(home: str | Path | None = None) -> tuple[str, ...]:
    """The folders a session may not write: where user units, user-manager environment and autostart entries live."""
    h = Path(home) if home else paths.home_dir()
    return tuple(str(h / rel) for rel in (*USER_UNIT_DIRS, *OTHER_PROTECTED))


# --- the sources and the check ---------------------------------------------------------------------------------------


def _git_ok(src: Path) -> bool:
    """Is the file tracked by git and unchanged from the commit? A file outside a repository, untracked or edited is not a
    committed source."""
    d = str(src.parent)
    try:
        tracked = subprocess.run(
            ["git", "-C", d, "ls-files", "--error-unmatch", "--", src.name],
            check=False,
            capture_output=True,
            timeout=10,
        )
        clean = subprocess.run(
            ["git", "-C", d, "diff", "--quiet", "HEAD", "--", src.name], check=False, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return tracked.returncode == 0 and clean.returncode == 0


def _expand(sources, home: Path) -> list[Path]:
    out = []
    for s in sources:
        p = Path(s.replace("~", str(home), 1)) if s.startswith("~") else Path(s)
        out.append(p)
    return out


def _template(unit: str) -> str:
    """An instance of a template unit (name, the at sign, an instance, the suffix) is read from the template file."""
    if "@" in unit:
        base, _, rest = unit.partition("@")
        suffix = "." + rest.rpartition(".")[2] if "." in rest else ""
        return f"{base}@{suffix}"
    return unit


def _dropin_source(source: Path, name: str) -> Path | None:
    for cand in (source / "dropins" / f"{name}.d", source / f"{name}.d"):
        if cand.is_dir():
            return cand
    return None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and not a.is_symlink() and b.is_file() and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _dropins_match(installed: Path, name: str, sources: list[Path]) -> bool:
    """Every file in the installed `NAME.d` folder is a committed drop-in, and none is missing or extra."""
    have = installed / f"{name}.d"
    if not os.path.lexists(have):
        return True  # no drop-in folder installed: nothing overrides the unit
    if not have.is_dir() or have.is_symlink():
        return False
    mine = sorted(p.name for p in have.iterdir())
    for s in sources:
        d = _dropin_source(s, name)
        if d is None:
            continue
        want = sorted(p.name for p in d.iterdir() if p.is_file())
        if mine == want and all(_same(have / n, d / n) and _git_ok(d / n) for n in want):
            return True
    return False


def unit_trusted(unit: str, sources, home: str | Path | None = None) -> bool:
    """True when `unit` may be started or enabled: no user-writable unit folder holds a file for it, or the one in
    `~/.config/systemd/user` is the committed source, drop-ins included. A path, or a file in any other user folder, is no."""
    h = Path(home) if home else paths.home_dir()
    if "/" in unit or unit.startswith("-") or not unit:
        return False  # `enable /tmp/x.service` links an arbitrary file
    name = _template(unit)
    srcs = _expand(sources or (), h)
    main = h / USER_UNIT_DIRS[0]
    for rel in USER_UNIT_DIRS[1:]:
        if os.path.lexists(h / rel / name) or os.path.lexists(h / rel / f"{name}.d"):
            return False
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and (
        os.path.lexists(Path(runtime) / "systemd/user" / name)
        or os.path.lexists(Path(runtime) / "systemd/user.control" / name)
    ):
        return False
    installed = main / name
    if not os.path.lexists(installed):
        return not os.path.lexists(main / f"{name}.d")  # a drop-in on a system unit changes what it runs
    for s in srcs:
        src = s / name
        if _same(installed, src) and _git_ok(src) and _dropins_match(main, name, srcs):
            return True
    return False


# --- the command rules -----------------------------------------------------------------------------------------------


def _verb_and_units(args: list[str]) -> tuple[str, list[str]]:
    toks, skip, verb, units = args, False, "", []
    for t in toks:
        if skip:
            skip = False
            continue
        if t in SYSTEMCTL_VALUED:
            skip = True
            continue
        if t.startswith("-"):
            continue
        if not verb:
            verb = t
        else:
            units.append(t)
    return verb, units


def refusal(canon: list[str], sources, home: str | Path | None = None) -> str | None:
    """The reason a command line (one segment, wrappers already stripped, flags normalised) is refused, or None."""
    if not canon:
        return None
    head = posixpath.basename(canon[0])
    args = canon[1:]
    if head == "loginctl" and "enable-linger" in args:
        return "loginctl enable-linger lets a user unit run with no session; ask the person to run it"
    if head == "systemd-run":
        for t in args:
            if t.startswith(RUN_PERSISTS) or t == "-r":
                return f"systemd-run {t.split('=')[0]} leaves a timer or unit behind, outside the gate; run it as a plain command or --scope"
        if "--no-block" in args and "--wait" not in args:
            return (
                "systemd-run --no-block without --wait leaves a process running outside the gate; add --wait or --scope"
            )
        return None
    if head != "systemctl" or "--user" not in args:
        return None
    verb, units = _verb_and_units([a for a in args if a != "--user"])
    if verb in NEVER_VERBS:
        return f"systemctl --user {verb} can start code outside the gate; install a unit with `ws unit install NAME` instead"
    if verb in START_VERBS:
        for u in units:
            if not unit_trusted(u, sources, home):
                return (
                    f"systemctl --user {verb} {u}: the unit is not the committed source (or is in a folder ws does not "
                    "install from); install it with `ws unit install`, then run this again"
                )
    return None


# --- ws unit install -------------------------------------------------------------------------------------------------


def install(name: str, sources, home: str | Path | None = None) -> dict:
    """Copy unit `name` (and its drop-ins) from the first committed source that has it into `~/.config/systemd/user`."""
    h = Path(home) if home else paths.home_dir()
    if (
        not name
        or "/" in name
        or name.startswith(".")
        or not name.endswith((".service", ".timer", ".socket", ".target", ".path", ".slice", ".mount"))
    ):
        raise WsError("bad_unit", f"{name!r} is not a unit file name", "give a name like ws-lead-heartbeat.timer")
    srcs = _expand(sources or (), h)
    if not srcs:
        raise WsError(
            "no_sources",
            "the policy names no [gate] unit_sources",
            "add the folder of committed units to [gate] unit_sources",
        )
    for s in srcs:
        src = s / name
        if not src.is_file():
            continue
        if not _git_ok(src):
            raise WsError(
                "not_committed", f"{src} is not committed and unchanged", "commit it, and let the lead join it, first"
            )
        main = h / USER_UNIT_DIRS[0]
        main.mkdir(parents=True, exist_ok=True)
        tmp = main / f".{name}.tmp"
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, main / name)
        copied = [name]
        d = _dropin_source(s, name)
        if d is not None:
            dst = main / f"{name}.d"
            dst.mkdir(exist_ok=True)
            for f in sorted(p for p in d.iterdir() if p.is_file()):
                if not _git_ok(f):
                    raise WsError(
                        "not_committed",
                        f"{f} is not committed and unchanged",
                        "commit it, and let the lead join it, first",
                    )
                (dst / f.name).write_bytes(f.read_bytes())
                copied.append(f"{name}.d/{f.name}")
            for (
                extra
            ) in dst.iterdir():  # a drop-in the source no longer has is removed, so the folder matches the source
                if extra.name not in {p.name for p in d.iterdir()}:
                    extra.unlink()
        return {
            "unit": name,
            "from": str(s),
            "copied": copied,
            "next": "systemctl --user daemon-reload, then enable it",
        }
    raise WsError(
        "no_such_unit",
        f"no unit {name} in the policy's unit_sources",
        "check the name, or add its folder to [gate] unit_sources",
    )


def cmd_install(args) -> int:
    from ws import policy

    pol = policy.load()
    output.result(install(args.name, pol.gate.unit_sources))
    return 0


def register(sub) -> None:
    u = sub.add_parser(
        "unit", help="install a committed systemd user unit (the only way one reaches ~/.config/systemd/user)"
    )
    s = u.add_subparsers(dest="unit_cmd", required=True)
    i = s.add_parser("install", help="copy a unit and its drop-ins from the policy's unit_sources")
    i.add_argument("name", help="the unit file name, for example ws-lead-heartbeat.timer")
    i.set_defaults(func=cmd_install)
