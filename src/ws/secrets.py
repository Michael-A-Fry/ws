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
import json
import os
import pwd
import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ws import osinfo
from ws.profile import ProfileError, load, profile_path, ws_home

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$")
HEADER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_VALUE = 8192
WALL_USER = "ws-broker"
WALL_GROUP = "ws-clients"
WALL_HOME = "/var/lib/ws-broker"
WALL_SOCKET = "/run/ws-broker/broker.sock"


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
            print(
                f"ws: {args.name!r} is not in {registry_path()}; add it with its hosts first, e.g.\n"
                f'    [{args.name}]\n    hosts = ["api.example.com"]\n    header = "x-api-key"',
                file=sys.stderr,
            )
            return 2
        value = check_value(_read_value(args.name))
        Store(store_dir()).write(args.name, value)
    except (SecretsError, EOFError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    except PermissionError:
        print(
            f"ws: the store {store_dir()} belongs to another user. Set it as that user: "
            f"`sudo -u {WALL_USER} ws secret set {args.name}`",
            file=sys.stderr,
        )
        return 1
    print(f"ws: set {args.name}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        rows, note = _rows(None)
    except SecretsError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"secrets": rows, "note": note}, indent=2))
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
        print(f"ws: {e}", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"ws: the store belongs to another user: `sudo -u {WALL_USER} ws secret rm {args.name}`", file=sys.stderr)
        return 1
    print(f"ws: removed {args.name}" if gone else f"ws: {args.name} was not set (0 changes)")
    return 0


def wall_commands(user: str, registry: Path, store: Path, ws_exe: str) -> str:
    """The one-time sudo steps that move the store to its own system user."""
    unit = f"""[Unit]
Description=ws secrets broker
After=network.target

[Service]
User={WALL_USER}
Group={WALL_USER}
Environment=WS_HOME={WALL_HOME}
ExecStart={ws_exe} broker serve --socket {WALL_SOCKET} --group {WALL_GROUP}
RuntimeDirectory=ws-broker
RuntimeDirectoryMode=0750
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={WALL_HOME}

[Install]
WantedBy=multi-user.target"""
    return f"""# One-time setup: the secrets move to the system user {WALL_USER}; the broker runs as it. After this, {user} and
# anything {user} runs (agents included) cannot read a value. They can ask the broker to use one, at its allowed hosts.
# Needs sudo and systemd. {ws_exe} must be runnable by {WALL_USER}: if it sits under a home folder, install ws system-wide first.

sudo groupadd --system --force {WALL_GROUP}
sudo useradd --system --home-dir {WALL_HOME} --shell /usr/sbin/nologin --create-home --groups {WALL_GROUP} {WALL_USER} \\
  || sudo usermod -aG {WALL_GROUP} {WALL_USER}
sudo usermod -aG {WALL_GROUP} {user}
sudo install -d -o {WALL_USER} -g {WALL_USER} -m 0700 {WALL_HOME} {WALL_HOME}/secrets {WALL_HOME}/logs
# the registry is owned by root so neither the broker nor an agent can add a host to it
sudo install -o root -g root -m 0644 {registry} {WALL_HOME}/secrets.toml
# move values you already set (skip if there are none), then re-check the modes
[ -d {store} ] && sudo sh -c 'mv {store}/* {WALL_HOME}/secrets/ 2>/dev/null; chown -R {WALL_USER}:{WALL_USER} {WALL_HOME}/secrets; chmod 0600 {WALL_HOME}/secrets/*'
sudo tee /etc/systemd/system/ws-broker.service >/dev/null <<'EOF'
{unit}
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ws-broker

# point your profile at it (the ws-secret adapter), then log out and in once so the new group applies:
#   [roles.secrets]
#   adapter = "ws-secret"
#   store = "{WALL_HOME}/secrets"
#   socket = "{WALL_SOCKET}"
# if you change the registry later, run the `install -o root` line again
# set or replace a value from now on with:  sudo -u {WALL_USER} env WS_HOME={WALL_HOME} {ws_exe} secret set NAME
# check it:  ws doctor   (WS104 should be ok, not warn)"""


def cmd_wall(args: argparse.Namespace) -> int:
    osi = osinfo.detect()
    if osi.kind == "linux" and osi.scheduler == "systemd":
        exe = shutil.which("ws") or f"{sys.executable} -m ws"
        print(wall_commands(getpass.getuser(), registry_path(), store_dir(), exe))
        return 0
    why = {
        "macos": "macOS needs a _ws-broker user and a launchd job; that is not built yet",
        "wsl": "WSL2 is not supported for the wall yet (it needs systemd enabled in the distro; a Windows-side user is not built)",
    }.get(osi.kind, "the wall needs Linux with systemd; not built for this machine yet")
    print(f"ws: {why}", file=sys.stderr)
    print(
        "ws: until then the store is protected only by file modes, and `ws doctor` WS104 stays at warn", file=sys.stderr
    )
    return 1


def register(sub) -> None:
    s = sub.add_parser("secret", help="the secrets registry and store (values are never shown)")
    ssub = s.add_subparsers(dest="secret_command", required=True)
    p = ssub.add_parser("set", help="store a value for a registered name (prompted, or from stdin)")
    p.add_argument("name")
    p.set_defaults(func=cmd_set)
    p = ssub.add_parser("list", help="names, hosts, set/unset and owner; never values")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = ssub.add_parser("rm", help="remove a stored value")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)
    p = ssub.add_parser("wall", help="print the one-time sudo steps that put the store behind its own system user")
    p.set_defaults(func=cmd_wall)
