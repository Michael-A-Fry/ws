"""`ws unit`: the one way a systemd user unit reaches the user manager, and the gate's rules about running one (C867).

A session that can write a user unit and start it runs code outside every PreToolUse hook (the manager starts it, not the
session). So the gate does these things, all here:

  * the folders systemd reads user units from (`~/.config/systemd` and `~/.local/share/systemd` as a whole, which also holds
    `user.conf`, plus `~/.config/environment.d` and `~/.config/autostart`; XDG_CONFIG_HOME and XDG_DATA_HOME are honoured) are
    protected paths: a session's own write to them is refused, and the wall makes them read-only (`unit_dirs`). So is every
    folder named in `[gate] unit_sources` (`source_paths`);
  * `ws unit install NAME` is the way in, and a session may not run it (it is an admin command, `agentsession.ADMIN_SUB`):
    the person runs it. It copies a unit and its drop-ins from a folder named in `[gate] unit_sources`, reading the bytes
    from git's objects on the branch `main` of the main checkout that holds the folder, never from the working tree, and only
    when that checkout is a real one: the folder is not (and holds no) symlink or nested repository, and its `.git` is a
    folder, not the file a worktree has. A file that git lists as a symlink is refused;
  * `systemctl --user enable|start|restart|...` passes only for a unit whose every file systemd would load for it, the unit
    file, its template, and every drop-in folder (`NAME.d`, the template's, the dash prefixes' and `service.d`), is byte for
    byte what the committed source has. A unit written in any other user-writable unit folder, a name that is a path, and
    any `--root`, `--machine` or `--host` are refused. A name that exists nowhere is a system unit, or does not exist, and
    passes; a name with no suffix is read as `.service`;
  * `systemd-run` is refused unless it is `--scope` (a scope is the caller's own child; a service, with or without `--wait`,
    is the manager's), and `loginctl enable-linger`, `crontab` (but `-l`), `at` and `batch` (but `-l` and `-c`) and
    `wsn timers` (which writes timers) are refused too.

What this cannot see: a program that writes a unit from inside its own code (`python -c`, `git -C` into a folder). The wall
closes that on Linux (the folders are read-only; seccomp also stops `systemctl --user` there); a separate Unix user for
agents closes it everywhere.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
from pathlib import Path

from ws import output, paths
from ws.output import WsError

COMMAND = "unit"

UNIT_REF = "main"  # the branch of the source checkout whose commits count: what the lead joined
SUFFIXES = (
    ".service",
    ".timer",
    ".socket",
    ".target",
    ".path",
    ".slice",
    ".mount",
    ".scope",
    ".automount",
    ".swap",
    ".device",
)
USER_UNIT_DIRS = (".config/systemd/user", ".config/systemd/user.control", ".local/share/systemd/user")

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
    {
        "link",
        "edit",
        "add-wants",
        "add-requires",
        "set-environment",
        "import-environment",
        "unset-environment",
        "preset",
    }
)
REFUSED_FLAGS = ("--root", "--machine", "--host", "-M", "-H", "--global")
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never"]


def _home(home) -> Path:
    return Path(home) if home else paths.home_dir()


def _xdg(name: str, default: str, home: Path) -> Path:
    v = os.environ.get(name, "")
    return Path(v) if v.startswith("/") else home / default


def unit_dirs(home: str | Path | None = None) -> tuple[str, ...]:
    """The folders a session may not write: everything systemd reads for the user manager, environment.d and autostart."""
    h = _home(home)
    cfg, data = _xdg("XDG_CONFIG_HOME", ".config", h), _xdg("XDG_DATA_HOME", ".local/share", h)
    out = [h / ".config/systemd", h / ".local/share/systemd", cfg / "systemd", data / "systemd"]
    out += [x / n for x in (h / ".config", cfg) for n in ("environment.d", "autostart")]
    return tuple(dict.fromkeys(str(p) for p in out))


def source_paths(pol) -> tuple[str, ...]:
    """The `[gate] unit_sources` folders as absolute paths: protected like the unit folders, so a session cannot edit what it
    would be installed from."""
    h = paths.home_dir()
    return tuple(str(Path(s.replace("~", str(h), 1)) if s.startswith("~") else Path(s)) for s in pol.gate.unit_sources)


def _user_dirs(h: Path) -> list[Path]:
    cfg, data = _xdg("XDG_CONFIG_HOME", ".config", h), _xdg("XDG_DATA_HOME", ".local/share", h)
    out = [cfg / "systemd/user", cfg / "systemd/user.control", data / "systemd/user"]
    out += [h / rel for rel in USER_UNIT_DIRS]
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        out += [Path(rt) / "systemd/user", Path(rt) / "systemd/user.control"]
    return list(dict.fromkeys(out))


# --- the committed source: git objects of the main checkout, never the working tree -------------------------------------------


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [*GIT, "-C", str(repo), *args],
            capture_output=True,
            timeout=15,
            check=False,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(paths.home_dir()),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _no_symlink(p: Path) -> bool:
    """True when no part of the path is a symlink."""
    return os.path.realpath(p) == os.path.abspath(p)


class Source:
    """One `unit_sources` folder: a folder of a main checkout, read through git objects on `main`."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.top: Path | None = None
        self.rel = ""
        if not folder.is_dir() or not _no_symlink(folder):
            return
        r = _run(folder, "rev-parse", "--show-toplevel")
        if r is None or r.returncode != 0:
            return
        top = Path(r.stdout.decode().strip())
        if not top.is_dir() or not (top / ".git").is_dir() or (top / ".git").is_symlink() or not _no_symlink(top):
            return  # a worktree has a .git file; only the main checkout counts
        if any(".git" in (*d, *f) for root, d, f in os.walk(folder) if Path(root) != top):
            return  # a nested repository inside the folder
        try:
            self.rel = folder.relative_to(top).as_posix()
        except ValueError:
            return
        branch = _run(top, "rev-parse", "--verify", "-q", f"refs/heads/{UNIT_REF}")
        if branch is None or branch.returncode != 0:
            return
        self.top = top

    def _path(self, name: str) -> str:
        return f"{self.rel + '/' if self.rel not in ('', '.') else ''}{name}"

    def blob(self, name: str) -> bytes | None:
        """The bytes of `name` (a path under the folder) on `main`, or None: not there, not a regular file, or no real source."""
        if self.top is None:
            return None
        path = self._path(name)
        tree = _run(self.top, "ls-tree", UNIT_REF, "--", path)
        if tree is None or tree.returncode != 0 or not tree.stdout.startswith((b"100644", b"100755")):
            return None  # a symlink (120000), a folder or nothing
        got = _run(self.top, "cat-file", "blob", f"{UNIT_REF}:{path}")
        return got.stdout if got is not None and got.returncode == 0 else None

    def is_folder(self, sub: str) -> bool:
        """True when `sub` is a folder on `main` (whatever it holds)."""
        if self.top is None:
            return False
        r = _run(self.top, "ls-tree", f"{UNIT_REF}:{self._path(sub)}")
        return r is not None and r.returncode == 0

    def names(self, sub: str) -> list[str] | None:
        """The names of the files directly under `sub` on `main`, or None when `sub` is not a folder there or holds anything
        but regular files."""
        if self.top is None:
            return None
        r = _run(self.top, "ls-tree", "-z", f"{UNIT_REF}:{self._path(sub)}")
        if r is None or r.returncode != 0:
            return None
        out = []
        for entry in r.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, name = entry.partition(b"\t")
            if not meta.startswith((b"100644", b"100755")):
                return None  # a symlink or a folder in a drop-in folder: not a committed drop-in set
            out.append(name.decode())
        return sorted(out)


def _sources(sources, h: Path) -> list[Source]:
    out = []
    for s in sources or ():
        out.append(Source(Path(s.replace("~", str(h), 1)) if s.startswith("~") else Path(s)))
    return out


def _dropin_dir(src: Source, name: str) -> str | None:
    for cand in (f"dropins/{name}.d", f"{name}.d"):
        if src.names(cand) is not None:
            return cand
    return None


# --- what systemd loads for a unit ---------------------------------------------------------------------------------------------


def _with_suffix(unit: str) -> str:
    return unit if unit.endswith(SUFFIXES) else unit + ".service"


def _template(unit: str) -> str:
    """An instance of a template (name, the at sign, an instance, the suffix) is read from the template file."""
    if "@" in unit:
        base, _, rest = unit.partition("@")
        suffix = "." + rest.rpartition(".")[2] if "." in rest else ""
        return f"{base}@{suffix}"
    return unit


def _loaded(unit: str) -> tuple[list[str], list[str]]:
    """(unit file names, drop-in folder names) systemd may read for `unit`: the instance and its template; for each, `NAME.d`,
    the dash-prefix folders (`a-.service.d` for `a-b.service`) and the type folder (`service.d`)."""
    tpl = _template(unit)
    files = list(dict.fromkeys([unit, tpl]))
    dirs: list[str] = []
    for n in files:
        base, _, suffix = n.rpartition(".")
        parts = base.split("-")
        dirs.append(f"{n}.d")
        dirs += [f"{'-'.join(parts[:i])}-.{suffix}.d" for i in range(1, len(parts))]
        dirs.append(f"{suffix}.d")
    return files, list(dict.fromkeys(dirs))


def _file_ok(installed: Path, src: Source, name: str) -> bool:
    want = src.blob(name)
    try:
        return want is not None and _no_symlink(installed) and installed.is_file() and installed.read_bytes() == want
    except OSError:
        return False


def _dir_ok(installed: Path, src: Source, name: str) -> bool:
    """The installed drop-in folder (`name` ends in `.d`) holds exactly the committed drop-ins of that name, byte for byte."""
    if not _no_symlink(installed) or not installed.is_dir():
        return False
    where = _dropin_dir(src, name[:-2])
    if where is None:
        return False
    want = src.names(where) or []
    have = sorted(p.name for p in installed.iterdir())
    return have == want and all(_file_ok(installed / n, src, f"{where}/{n}") for n in want)


def unit_trusted(unit: str, sources, home: str | Path | None = None) -> bool:
    """True when `unit` may be started or enabled: nothing systemd would load for it sits in a user-writable unit folder,
    or everything that does is the committed source (the file, its template, and every drop-in folder). A path, or anything
    in a folder other than `~/.config/systemd/user`, is not."""
    h = _home(home)
    if "/" in unit or unit.startswith("-") or not unit:
        return False  # `enable /tmp/x.service` links an arbitrary file
    unit = _with_suffix(unit)
    files, dirs = _loaded(unit)
    main = _xdg("XDG_CONFIG_HOME", ".config", h) / "systemd/user"
    srcs = [s for s in _sources(sources, h) if s.top is not None]
    for d in _user_dirs(h):
        if d == main:
            continue
        if any(os.path.lexists(d / n) for n in (*files, *dirs)):
            return False
    for n in files:
        if os.path.lexists(main / n) and not any(_file_ok(main / n, s, n) for s in srcs):
            return False
    for n in dirs:
        if os.path.lexists(main / n) and not any(_dir_ok(main / n, s, n) for s in srcs):
            return False
    return True


# --- the command rules -----------------------------------------------------------------------------------------------------


def refusal(canon: list[str], sources, home: str | Path | None = None) -> str | None:
    """The reason a command line (one segment, wrappers already stripped, flags normalised) is refused, or None."""
    if not canon:
        return None
    head = posixpath.basename(canon[0])
    args = canon[1:]
    if head == "loginctl" and "enable-linger" in args:
        return "loginctl enable-linger lets a user unit run with no session; ask the person to run it"
    if head == "crontab" and args != ["-l"]:
        return "crontab runs code outside the gate; only `crontab -l` is for a session"
    if head in ("at", "batch") and not (args and args[0] in ("-l", "-c")):
        return f"{head} runs code outside the gate; only `{head} -l` and `-c` are for a session"
    if head == "wsn" and "timers" in args and any(a in args for a in ("apply", "install")):
        return "wsn timers writes systemd timers; run it from your own terminal"
    if head == "systemd-run":
        if "--scope" in args:
            return None  # a scope is the caller's own child, run through this gate
        return "systemd-run starts a service the manager runs outside the gate; only `--scope` is for a session"
    if head != "systemctl" or "--user" not in args:
        return None
    rest = [a for a in args if a != "--user"]
    if any(a == f or a.startswith(f + "=") for a in rest for f in REFUSED_FLAGS):
        return (
            "systemctl --user with --root, --machine, --host or --global reads units from somewhere ws does not check"
        )
    verbs = [a for a in rest if a in START_VERBS | NEVER_VERBS]
    if not verbs:
        return None
    if any(v in NEVER_VERBS for v in verbs):
        return f"systemctl --user {verbs[0]} can start code outside the gate; the person installs a unit with `ws unit install NAME`"
    verb = verbs[0]
    for u in (a for a in rest if not a.startswith("-") and a not in START_VERBS):
        if not unit_trusted(u, sources, home):
            return (
                f"systemctl --user {verb} {u}: the unit is not the committed source (or is in a folder ws does not "
                "install from); the person installs it with `ws unit install`"
            )
    return None


# --- ws unit install ----------------------------------------------------------------------------------------------------------


def install(name: str, sources, home: str | Path | None = None) -> dict:
    """Copy unit `name` (and its drop-ins) from the first committed source that has it into `~/.config/systemd/user`. Everything
    is read and checked from git first, and nothing is written until all of it passes."""
    h = _home(home)
    if not name or "/" in name or name.startswith(".") or not name.endswith(SUFFIXES):
        raise WsError("bad_unit", f"{name!r} is not a unit file name", "give a name like ws-lead-heartbeat.timer")
    if not sources:
        raise WsError(
            "no_sources",
            "the policy names no [gate] unit_sources",
            "add the folder of committed units to [gate] unit_sources",
        )
    srcs = _sources(sources, h)
    for s in srcs:
        if s.top is None:
            raise WsError(
                "bad_source",
                f"{s.folder} is not a folder of a main checkout (a symlink, a nested repository, a worktree, or no `{UNIT_REF}` branch)",
                "fix [gate] unit_sources",
            )
    for s in srcs:
        unit_bytes = s.blob(name)
        if unit_bytes is None:
            continue
        plan: list[tuple[str, bytes]] = [(name, unit_bytes)]
        for cand in (f"dropins/{name}.d", f"{name}.d"):
            if s.is_folder(cand) and s.names(cand) is None:
                raise WsError(
                    "not_committed",
                    f"{cand} holds something that is not a regular committed file on {UNIT_REF}",
                    "commit plain files only",
                )
        where = _dropin_dir(s, name)
        if where is not None:
            for n in s.names(where) or []:
                b = s.blob(f"{where}/{n}")
                if b is None:
                    raise WsError(
                        "not_committed",
                        f"{where}/{n} is not a regular committed file on {UNIT_REF}",
                        "commit it, and let the lead join it, first",
                    )
                plan.append((f"{name}.d/{n}", b))
        main = h / USER_UNIT_DIRS[0]
        main.mkdir(parents=True, exist_ok=True)
        staged = []
        for rel, data in plan:  # all the writing is after all the checking
            dest = main / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.tmp")
            tmp.write_bytes(data)
            staged.append((tmp, dest))
        for tmp, dest in staged:
            os.replace(tmp, dest)
        if where is not None:
            keep = {Path(rel).name for rel, _ in plan[1:]}
            for extra in (main / f"{name}.d").iterdir():  # a drop-in the source no longer has is removed
                if extra.name not in keep:
                    extra.unlink()
        return {
            "unit": name,
            "from": str(s.folder),
            "copied": [rel for rel, _ in plan],
            "next": "systemctl --user daemon-reload, then enable it",
        }
    raise WsError(
        "no_such_unit",
        f"no unit {name} on {UNIT_REF} in the policy's unit_sources",
        "check the name, or add its folder to [gate] unit_sources",
    )


def cmd_install(args) -> int:
    from ws import policy

    pol = policy.load()
    output.result(install(args.name, pol.gate.unit_sources))
    return 0


def register(sub) -> None:
    u = sub.add_parser("unit", help="install a committed systemd user unit (the only way one reaches the user manager)")
    s = u.add_subparsers(dest="unit_cmd", required=True)
    i = s.add_parser("install", help="copy a unit and its drop-ins from the policy's unit_sources (a person's command)")
    i.add_argument("name", help="the unit file name, for example ws-lead-heartbeat.timer")
    i.set_defaults(func=cmd_install)
