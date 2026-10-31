"""The secrets registry and store, and `ws secret`.

The registry (`$WS_HOME/secrets.toml`) is not secret. It says which names exist and where each may be used:

    [anthropic]
    hosts = ["api.anthropic.com"]
    header = "x-api-key"            # or "authorization" with format = "Bearer {value}"
    purposes = ["run"]              # empty or missing = any purpose

The store holds the values: one file per name, mode 0600, in a directory of mode 0700. Agents never read it. The broker
(ws.broker) reads it and injects the value at the one allowed host. A value is never printed, logged or put in an error.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ws import osinfo, output
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path, ws_home

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$")
HEADER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_VALUE = 8192
WALL_USER = "ws-broker"
WALL_GROUP = "ws-clients"
WALL_HOME = "/var/lib/ws-broker"
WALL_SOCKET = "/run/ws-broker/broker.sock"
WALL_BIN = "/opt/ws/bin/ws"  # a root-owned launcher: the broker never runs code a normal user can write
WALL_LIB = "/opt/ws/lib"
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./+-]+$")


class SecretsError(Exception):
    """Never carries a secret value."""


@dataclass(frozen=True)
class Entry:
    name: str
    hosts: tuple[str, ...] = ()
    header: str = "x-api-key"
    format: str = "{value}"
    purposes: tuple[str, ...] = field(default_factory=tuple)


def registry_path(env: dict | None = None) -> Path:
    return ws_home(env) / "secrets.toml"


def _profile_secrets(env: dict | None) -> dict:
    try:
        prof = load(profile_path(env)) or {}
    except ProfileError as e:
        raise SecretsError(str(e)) from e
    roles = prof.get("roles")
    cfg = roles.get("secrets") if isinstance(roles, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def store_dir(env: dict | None = None, cfg: dict | None = None) -> Path:
    """`[roles.secrets] store` in the profile (or the `cfg` already read from it), else `$WS_HOME/secrets`."""
    s = (_profile_secrets(env) if cfg is None else cfg).get("store")
    if isinstance(s, str) and s:
        return Path(s).expanduser()
    return ws_home(env) / "secrets"


def socket_path(env: dict | None = None, cfg: dict | None = None) -> Path:
    """The broker socket a client uses: $WS_BROKER_SOCKET, else `[roles.secrets] socket`, else `$WS_HOME/run/broker.sock`."""
    env = os.environ if env is None else env
    if env.get("WS_BROKER_SOCKET"):
        return Path(env["WS_BROKER_SOCKET"]).expanduser()
    s = (_profile_secrets(env) if cfg is None else cfg).get("socket")
    if isinstance(s, str) and s:
        return Path(s).expanduser()
    return ws_home(env) / "run" / "broker.sock"


def load_registry(path: Path) -> dict[str, Entry]:
    """The registry. A missing file is an empty registry; a file that does not parse, or a bad entry, raises."""
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        raise SecretsError(f"{path}: {e}") from e
    out: dict[str, Entry] = {}
    for name, t in raw.items():
        if not NAME_RE.match(name):
            raise SecretsError(f"{path}: {name!r} is not a valid name (letters, digits, - and _; starts with a letter)")
        if not isinstance(t, dict):
            raise SecretsError(f"{path}: [{name}] must be a table")
        out[name] = _entry(path, name, t)
    return out


def _str_list(path: Path, name: str, key: str, v: object) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SecretsError(f"{path}: [{name}] {key} must be a list of strings")
    return tuple(v)


def _entry(path: Path, name: str, t: dict) -> Entry:
    hosts = tuple(h.lower() for h in _str_list(path, name, "hosts", t.get("hosts", [])))
    for h in hosts:
        if not HOST_RE.match(h):
            raise SecretsError(f"{path}: [{name}] {h!r} is not a host name (no scheme, no path)")
    header = t.get("header", "x-api-key")
    if not isinstance(header, str) or not HEADER_RE.match(header.lower()):
        raise SecretsError(f'{path}: [{name}] header must be a header name such as "x-api-key"')
    header = header.lower()
    fmt = t.get("format", "Bearer {value}" if header == "authorization" else "{value}")
    if not isinstance(fmt, str) or fmt.count("{value}") != 1 or re.search(r"[\x00-\x1f\x7f]", fmt):
        raise SecretsError(f"{path}: [{name}] format must contain {{value}} once, on one line")
    purposes = _str_list(path, name, "purposes", t.get("purposes", []))
    return Entry(name=name, hosts=hosts, header=header, format=fmt, purposes=purposes)


def check_value(value: str) -> str:
    """The value as stored. Raises SecretsError (without the value) if it could not be a header value."""
    if not value:
        raise SecretsError("the value is empty")
    if len(value) > MAX_VALUE:
        raise SecretsError(f"the value is longer than {MAX_VALUE} characters")
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise SecretsError("the value has a line break or control character")
    return value


class Store:
    """One file per name (mode 0600) in a directory of mode 0700. Writes are atomic."""

    def __init__(self, path: Path):
        self.path = path

    def _file(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise SecretsError("not a valid secret name")
        return self.path / name

    def read(self, name: str) -> str:
        """The value. Raises FileNotFoundError if unset, SecretsError if the store cannot be trusted."""
        f = self._file(name)
        try:
            st = self.path.stat()
        except FileNotFoundError as e:
            raise SecretsError("the store directory does not exist") from e
        except OSError as e:
            raise SecretsError("the store directory cannot be read") from e
        if st.st_mode & 0o077:
            raise SecretsError("the store directory is open to other users")
        try:
            with f.open("rb") as fh:
                fst = os.fstat(fh.fileno())
                if not stat.S_ISREG(fst.st_mode) or fst.st_mode & 0o077:
                    raise SecretsError("a store file is open to other users")
                data = fh.read(MAX_VALUE * 4 + 1)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise SecretsError("a store file cannot be read") from e
        try:
            return check_value(data.decode("utf-8"))
        except (UnicodeDecodeError, SecretsError) as e:
            raise SecretsError("a store file does not hold a usable value") from e

    def write(self, name: str, value: str) -> None:
        f = self._file(name)
        check_value(value)
        self._ensure_dir()
        tmp = self.path / f".{name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(value.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, f)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _ensure_dir(self) -> None:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.path.stat()
        if st.st_mode & 0o077:
            if st.st_uid != os.geteuid():
                raise SecretsError(f"{self.path} is open to other users and is not yours to fix")
            self.path.chmod(0o700)

    def delete(self, name: str) -> bool:
        try:
            self._file(name).unlink()
        except FileNotFoundError:
            return False
        return True

    def names(self) -> list[str]:
        """Names with a file. Raises PermissionError if the store belongs to another user."""
        try:
            return sorted(p.name for p in self.path.iterdir() if NAME_RE.match(p.name) and p.is_file())
        except FileNotFoundError:
            return []

    def loose(self) -> list[str]:
        """Problems with permissions, as plain sentences (no values). Raises PermissionError if not ours to see."""
        out = []
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return out
        if st.st_mode & 0o077:
            out.append(f"directory mode is {st.st_mode & 0o777:04o}, not 0700")
        for n in self.names():
            m = (self.path / n).stat().st_mode & 0o777
            if m & 0o077:
                out.append(f"{n} mode is {m:04o}, not 0600")
        return out


def health(reg_path: Path, store: Store) -> tuple[list[str], int | None]:
    """What is wrong with the registry and the store, as plain sentences (never a value), and the store's owner uid.
    Raises PermissionError if the store belongs to another user: the caller then asks the broker."""
    problems = []
    if not reg_path.is_file():
        problems.append(f"no registry at {reg_path}")
    try:
        reg = load_registry(reg_path)
    except SecretsError as e:
        problems.append(f"registry unreadable: {e}")
        reg = {}
    problems += [f"{e.name} has no hosts, so it can never be used" for e in reg.values() if not e.hosts]
    try:
        owner = store.path.stat().st_uid
    except FileNotFoundError:
        return [*problems, f"no store at {store.path}"], None
    return [*problems, *store.loose()], owner


def owner_name(uid: int) -> str:
    import pwd  # POSIX only: not at the top so `ws doctor` still runs on native Windows

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _read_value(name: str) -> str:
    if sys.stdin.isatty():
        v = getpass.getpass(f"Value for {name} (not shown): ")
    else:
        v = sys.stdin.read(MAX_VALUE * 4 + 1)
        if v.endswith("\r\n"):
            v = v[:-2]
        elif v.endswith("\n"):
            v = v[:-1]
    return v


def _rows(env: dict | None) -> tuple[list[dict], str]:
    """One row per registered name: name, hosts, header, state (set|unset|unknown), owner. Never a value."""
    reg = load_registry(registry_path(env))
    store = Store(store_dir(env))
    walled = False
    try:
        present = set(store.names())
    except PermissionError:
        present, walled = set(), True
    try:
        owner = owner_name(store.path.stat().st_uid)
    except OSError:
        owner = "?"
    rows = []
    for e in reg.values():
        state = "unknown" if walled else ("set" if e.name in present else "unset")
        o = owner
        if state == "set":
            with contextlib.suppress(OSError):
                o = owner_name((store.path / e.name).stat().st_uid)
        rows.append({"name": e.name, "hosts": list(e.hosts), "header": e.header, "state": state, "owner": o})
    note = ""
    if walled:
        note = "the store belongs to another user, so set/unset cannot be read from here (that is the wall working)"
    return rows, note


def cmd_set(args: argparse.Namespace) -> int:
    try:
        reg = load_registry(registry_path())
        if args.name not in reg:
            raise WsError(
                "not_found",
                f"{args.name!r} is not in {registry_path()}",
                f'add it with its hosts first, e.g.\n    [{args.name}]\n    hosts = ["api.example.com"]\n    header = "x-api-key"',
                2,
            )
        value = check_value(_read_value(args.name))
        Store(store_dir()).write(args.name, value)
    except (SecretsError, EOFError) as e:
        raise WsError("failed", str(e) or "no value given", "see `ws help secret`") from e
    except PermissionError as e:
        raise WsError(
            "refused",
            f"the store {store_dir()} belongs to another user",
            f"set it as that user: `{wall_hint('set', args.name)}`",
        ) from e
    if args.json:
        output.result({"name": args.name, "set": True})
        return 0
    print(f"ws: set {args.name}")
    return 0


_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _env_value(path: Path, var: str | None) -> str:
    """The raw value from an env file of `VAR=value` lines (blank lines and # comments skipped, one pair of
    surrounding quotes stripped). With several lines, `var` says which. Every error names the file and a line number,
    never a line's text."""
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_VALUE * 64 + 1)
    except OSError as e:
        raise SecretsError(f"{path} cannot be read") from e
    if len(data) > MAX_VALUE * 64:
        raise SecretsError(f"{path} is too big for an env file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SecretsError(f"{path} is not UTF-8 text") from e
    found: list[tuple[int, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            raise SecretsError(f"{path}: line {n} is not VAR=value")
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        found.append((n, m.group(1), v))
    if not found:
        raise SecretsError(f"{path} has no VAR=value line")
    if var is None:
        if len(found) > 1:
            raise SecretsError(
                f"{path} has {len(found)} VAR=value lines (lines {found[0][0]} to {found[-1][0]}); use --var"
            )
        return found[0][2]
    hits = [f for f in found if f[1] == var]
    if not hits:
        raise SecretsError(f"{path} has no {var} line")
    return hits[-1][2]


def cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.from_env).expanduser()
    try:
        reg = load_registry(registry_path())
        if args.name not in reg:
            raise WsError(
                "not_found",
                f"{args.name!r} is not in {registry_path()}",
                f'add it with its hosts first, e.g.\n    [{args.name}]\n    hosts = ["api.example.com"]\n    header = "x-api-key"',
                2,
            )
        value = check_value(_env_value(src, args.var))
        Store(store_dir()).write(args.name, value)
    except SecretsError as e:
        raise WsError(
            "failed",
            str(e),
            "the env file needs one VAR=value line, or several with --var VAR; the value is never shown",
        ) from e
    except PermissionError as e:
        raise WsError(
            "refused",
            f"the store {store_dir()} belongs to another user",
            f"set it as that user: `{wall_hint('set', args.name)}`",
        ) from e
    removed = False
    if args.delete_source:
        try:
            src.unlink()
            removed = True
        except OSError as e:
            raise WsError(
                "failed", f"{args.name} was stored, but {src} could not be removed", "remove it yourself"
            ) from e
    if args.json:
        output.result({"name": args.name, "set": True, "source_removed": removed})
        return 0
    print(f"ws: set {args.name}" + (f" (removed {src})" if removed else ""))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        rows, note = _rows(None)
    except SecretsError as e:
        raise WsError("bad_config", str(e), f"fix {registry_path()}") from e
    if args.json:
        output.result({"secrets": rows, "note": note})
        return 0
    if not rows:
        print(f"ws: nothing registered in {registry_path()}")
        return 0
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        print(
            f"{r['name']:<{w}}  {r['state']:<7}  {r['owner']:<10}  {r['header']}  {', '.join(r['hosts']) or '(no hosts)'}"
        )
    if note:
        print(f"ws: {note}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    try:
        gone = Store(store_dir()).delete(args.name)
    except SecretsError as e:
        raise WsError("failed", str(e)) from e
    except PermissionError as e:
        raise WsError("refused", "the store belongs to another user", f"`{wall_hint('rm', args.name)}`") from e
    if args.json:
        output.result({"name": args.name, "removed": gone})
        return 0
    print(f"ws: removed {args.name}" if gone else f"ws: {args.name} was not set (0 changes)")
    return 0


def wall_hint(verb: str, name: str) -> str:
    """The command that changes the walled store: as the broker user, with its WS_HOME (sudo drops the environment)."""
    return f"sudo -u {WALL_USER} env WS_HOME={WALL_HOME} {WALL_BIN} secret {verb} {shlex.quote(name)}"


def untrusted_reason(path: str | Path, tree: bool = False) -> str | None:
    """Why code at `path` could be changed by a non-root user, or None. Every parent of the real path must be owned by
    root and not writable by group or others; with `tree`, so must every file and folder below it (no symlinks)."""
    real = Path(os.path.realpath(path))
    try:
        for q in [real, *real.parents]:
            st = q.stat()
            if st.st_uid != 0:
                return f"{q} is owned by {owner_name(st.st_uid)}, not root"
            if st.st_mode & 0o022:
                return f"{q} can be written by users other than root"
        if tree and real.is_dir():
            for dirpath, dirs, files in os.walk(real):
                for n in [*dirs, *files]:
                    q = Path(dirpath) / n
                    st = q.lstat()
                    if stat.S_ISLNK(st.st_mode):
                        return f"{q} is a symlink"
                    if st.st_uid != 0 or st.st_mode & 0o022:
                        return f"{q} is not root-owned and closed to others"
    except OSError as e:
        return f"{real}: {type(e).__name__}"
    return None


def trusted_python() -> tuple[str | None, str]:
    """A Python 3.11+ that only root can change, as (path, "") or (None, why not)."""
    why = []
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        real = os.path.realpath(cand)
        if not os.path.isfile(real):
            continue
        bad = untrusted_reason(real) or (None if SAFE_PATH.match(real) else f"{real} has unusual characters")
        if bad:
            why.append(bad)
            continue
        ok = subprocess.run([real, "-c", "import sys; sys.exit(sys.version_info < (3, 11))"], check=False)
        if ok.returncode == 0:
            # keep the stable name (python3 survives a package update; python3.12 does not) if its folder is root's too
            return (cand if untrusted_reason(os.path.dirname(cand)) is None else real), ""
        why.append(f"{real} is older than Python 3.11")
    return None, "; ".join(why) or "no python3 found in /usr/bin or /usr/local/bin"


PY_FLAGS = "-P -s"  # no current folder and no user site on sys.path: a planted ./ws/ is never imported
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def launcher_script(py: str, lib: str = WALL_LIB) -> str:
    """The root-owned launcher. It leaves the caller's folder first, so nothing there can shadow the installed ws."""
    return f'#!/bin/sh\ncd /\nPYTHONPATH={lib} PYTHONDONTWRITEBYTECODE=1 exec {py} {PY_FLAGS} -m ws "$@"\n'


def tree_digest(pkg_dir: Path) -> str:
    """sha256 over the sorted `sha256sum` lines of every regular file below pkg_dir (not __pycache__), the same value
    the wall script computes on the root-owned snapshot, so the two can be compared. Raises SecretsError on a symlink,
    a special file or a file that is not .py."""
    lines = []
    for dirpath, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in [*dirs, *files]:
            q = Path(dirpath) / n
            if q.is_symlink() or not (q.is_dir() or q.is_file()):
                raise SecretsError(f"{q} is a symlink or special file; the copy of ws would not be a plain snapshot")
        for n in files:
            q = Path(dirpath) / n
            if not n.endswith(".py"):
                raise SecretsError(f"{q} is not a .py file; the copy of ws would not be a plain snapshot")
            rel = "./" + q.relative_to(pkg_dir).as_posix()
            lines.append((rel.encode(), hashlib.sha256(q.read_bytes()).hexdigest()))
    lines.sort()
    text = "".join(f"{h}  {rel.decode()}\n" for rel, h in lines)
    return hashlib.sha256(text.encode()).hexdigest()


def git_revision(pkg_dir: Path) -> str:
    """Where this copy of ws came from, for the person to compare with GitHub."""
    try:
        head = subprocess.run(
            ["git", "-C", str(pkg_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10
        )
        if head.returncode != 0:
            return "not a git checkout (compare the tree digest with a signed release instead)"
        dirty = subprocess.run(
            ["git", "-C", str(pkg_dir), "status", "--porcelain", "--", str(pkg_dir)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        state = "with uncommitted changes" if dirty.stdout.strip() else "clean"
        return f"git commit {head.stdout.strip()} ({state})"
    except (OSError, subprocess.SubprocessError):
        return "unknown (git did not answer)"


def wall_commands(user: str, registry: Path, old_store: Path, py: str, pkg_dir: Path, names: list[str]) -> str:
    """The one-time sudo steps that move the store to its own system user. Every path from the user's side is quoted.

    Trust on first use: the root-owned copy of ws is a snapshot of `pkg_dir` taken when the script runs. The script
    prints the git commit and a tree digest so the person can compare them with GitHub, and stops if the files it
    copies do not match the digest printed here. It cannot prove the checkout is the real ws: installing from a signed
    release tag (phase T) is what closes that gap. Until then this trusts the checkout at the moment of install.
    """
    q = shlex.quote
    digest = tree_digest(pkg_dir)
    source = git_revision(pkg_dir)
    unit = f"""[Unit]
Description=ws secrets broker
After=network.target

[Service]
User={WALL_USER}
Group={WALL_USER}
WorkingDirectory=/
Environment=WS_HOME={WALL_HOME}
ExecStart={WALL_BIN} broker serve --socket {WALL_SOCKET} --group {WALL_GROUP}
RuntimeDirectory=ws-broker
RuntimeDirectoryMode=0750
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={WALL_HOME}

[Install]
WantedBy=multi-user.target"""
    rotate = "\n".join(wall_hint("set", n) for n in names)
    return f"""#!/bin/bash
# One-time setup. Secrets move to the system user {WALL_USER}, and the broker runs as it, from a copy of ws that only
# root can change (so nothing running as {user} can edit the code that holds the keys). Needs sudo and systemd.
# Save this as a file and run it with `bash`: it stops at the first step that fails, so do not paste it into a shell.
set -euo pipefail

# WHAT THIS TRUSTS. The copy of ws below is whatever {q(str(pkg_dir))} holds at the moment you run it.
#   source:      {source}
#   tree sha256: {digest}
# Compare the commit with GitHub. The script stops if the files it copies do not match that digest. It cannot prove the
# checkout is the real ws: installing from a signed release tag (not built yet) is what closes that. Until then you are
# trusting this checkout at the moment of install.
# Also read the registry first: it is copied as is, and it decides which hosts each key may be sent to.
#   {q(str(registry))}

# ws: one snapshot taken by root without following links, checked, then moved into place. The system Python runs it.
sudo install -d -o root -g root -m 0755 /opt/ws /opt/ws/bin {WALL_LIB}
STAGE=$(sudo mktemp -d /opt/ws/.stage.XXXXXX)
trap 'sudo rm -rf "$STAGE"' EXIT
sudo cp -RP --no-preserve=all -- {q(str(pkg_dir))} "$STAGE/ws"
sudo find "$STAGE/ws" -name __pycache__ -type d -prune -exec rm -rf {{}} +
if [ -n "$(sudo find "$STAGE" -mindepth 1 ! -type f ! -type d -print -quit)" ]; then
  echo "ws: the snapshot has a symlink or special file; nothing installed" >&2; exit 1
fi
if [ -n "$(sudo find "$STAGE/ws" -type f ! -name '*.py' -print -quit)" ]; then
  echo "ws: the snapshot has a file that is not .py; nothing installed" >&2; exit 1
fi
sudo chown -R root:root "$STAGE"
sudo chmod -R u=rwX,go=rX "$STAGE"
DIGEST=$(cd "$STAGE/ws" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum | sha256sum | cut -d' ' -f1)
if [ "$DIGEST" != {digest} ]; then
  echo "ws: the source changed since this was printed (tree sha256 $DIGEST); nothing installed" >&2; exit 1
fi
echo "ws: installing ws tree sha256 $DIGEST"
sudo rm -rf {WALL_LIB}/ws
sudo mv "$STAGE/ws" {WALL_LIB}/ws
sudo tee {WALL_BIN} >/dev/null <<'EOF-LAUNCHER'
{launcher_script(py).rstrip()}
EOF-LAUNCHER
sudo chown root:root {WALL_BIN}
sudo chmod 0755 {WALL_BIN}

sudo groupadd --system --force {WALL_GROUP}
id {WALL_USER} >/dev/null 2>&1 || sudo useradd --system --home-dir {WALL_HOME} --no-create-home --shell /usr/sbin/nologin {WALL_USER}
sudo usermod -aG {WALL_GROUP} {WALL_USER}
sudo usermod -aG {WALL_GROUP} {q(user)}

# the store and logs belong to {WALL_USER}; the registry belongs to root so neither the broker nor an agent can add a host
sudo install -d -o {WALL_USER} -g {WALL_USER} -m 0700 {WALL_HOME} {WALL_HOME}/secrets {WALL_HOME}/logs
sudo install -o root -g root -m 0644 {q(str(registry))} {WALL_HOME}/secrets.toml

sudo tee /etc/systemd/system/ws-broker.service >/dev/null <<'EOF-UNIT'
{unit}
EOF-UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now ws-broker

# Values in the old store ({q(str(old_store))}) were readable by everything that ran as {user}, agents included.
# They are not moved. Make a new key at each provider, revoke the old one, then paste the new one here:
{rotate or "# (no names are registered yet)"}
# and delete the old store:  rm -r {q(str(old_store))}

# Point your profile at the broker, then log out and in once so the new group applies:
#   [roles.secrets]
#   adapter = "ws-secret"
#   store = "{WALL_HOME}/secrets"
#   socket = "{WALL_SOCKET}"
# Registry changes later: review the file, then run the `install -o root` line above again.
# Check it: ws doctor  (WS104 should be ok, not warn)
"""


def cmd_wall(args: argparse.Namespace) -> int:
    osi = osinfo.detect()
    if osi.kind == "linux" and osi.scheduler == "systemd":
        import pwd

        if os.getuid() == 0:
            raise WsError("refused", "run this as your normal user, not root", "it says which user to put in the group")
        py, why = trusted_python()
        if py is None:
            raise WsError(
                "unavailable",
                f"the broker must run on a Python that only root can change, and there is none: {why}",
                "install the system python3 (3.11 or newer), e.g. `sudo apt install python3`, then run this again",
            )
        where = Path(WALL_LIB)
        while not where.exists():
            where = where.parent
        if (bad := untrusted_reason(where)) is not None:
            raise WsError(
                "refused",
                f"the copy of ws would go under {where}, which others can change: {bad}",
                f"fix that first (for example `sudo chown root:root {where} && sudo chmod go-w {where}`), then run this again",
            )
        pkg_dir = Path(__file__).resolve().parent
        registry, old_store = registry_path(), store_dir()
        for p in (registry, old_store, pkg_dir):
            if CONTROL_RE.search(str(p)):
                raise WsError(
                    "bad_input",
                    "a path has a line break or control character in it; refusing to print commands for it",
                    "move or rename that folder",
                )
        try:
            names = sorted(load_registry(registry))
            text = wall_commands(pwd.getpwuid(os.getuid()).pw_name, registry, old_store, py, pkg_dir, names)
        except SecretsError as e:
            raise WsError("bad_config", str(e), f"fix {registry}") from e
        print(text, end="")
        return 0
    why = {
        "macos": "macOS needs a _ws-broker user and a launchd job; that is not built yet",
        "wsl": "WSL2 is not supported for the wall yet (it needs systemd enabled in the distro; a Windows-side user is not built)",
    }.get(osi.kind, "the wall needs Linux with systemd; not built for this machine yet")
    raise WsError(
        "unsupported", why, "until then the store is protected only by file modes, and `ws doctor` WS104 stays at warn"
    )


def register(sub) -> None:
    s = sub.add_parser("secret", help="the secrets registry and store (values are never shown)")
    ssub = s.add_subparsers(dest="secret_command", required=True)
    p = ssub.add_parser("set", help="store a value for a registered name (prompted, or from stdin)")
    p.add_argument("name")
    p.set_defaults(func=cmd_set)
    p = ssub.add_parser("import", help="store a value for a registered name from an env file (VAR=value)")
    p.add_argument("name")
    p.add_argument("--from-env", required=True, metavar="PATH", help="the env file to read")
    p.add_argument("--var", metavar="VAR", help="which variable, when the file has several lines")
    p.add_argument("--delete-source", action="store_true", help="remove the env file after the value is stored")
    p.set_defaults(func=cmd_import)
    p = ssub.add_parser("list", help="names, hosts, set/unset and owner; never values")
    p.set_defaults(func=cmd_list)
    p = ssub.add_parser("rm", help="remove a stored value")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)
    p = ssub.add_parser("wall", help="print the one-time sudo steps that put the store behind its own system user")
    p.set_defaults(func=cmd_wall)
