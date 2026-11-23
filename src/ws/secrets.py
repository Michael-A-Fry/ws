"""The secrets registry and store, and `ws secret`.

The registry (`secrets.toml` in the config folder) is not secret. It says which names exist and where each may be used:

    [anthropic]
    hosts = ["api.anthropic.com"]
    header = "x-api-key"            # or "authorization" with format = "Bearer {value}"
    purposes = ["run"]              # empty or missing = any purpose
    tier = "worker"                 # optional: walled | worker | lead | lead-elevated (see below)
    area = "home"                   # optional: only runs of this area may lease it
    source = "paste"                # optional: paste (a person brings it; the default) or generate (ws makes it)

A `source = "generate"` name is one only ws needs to know, such as a backup password: `ws secret generate NAME` makes a
strong random value and nobody types or pastes it. It needs no hosts. `ws secret reveal NAME` shows it to its owner
once, on their own device (ws.reveal), so they can keep a copy for a restore.

A name with a `tier` or an `area` is scoped: the broker leases it only to a run whose broker-side run record (ws.broker)
declares it, in a tier that reaches it and in the same area. `lead-elevated` names are the elevated ones that
`ws elevate off` stops at once. A name with neither is unscoped and keeps the older rule (any allowed caller), unless the
broker is started with `--require-run`.

The store holds the values: one file per name, mode 0600, in a directory of mode 0700. Agents never read it. The broker
(ws.broker) reads it and injects the value at the one allowed host. A value is never printed, logged or put in an error.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
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

from ws import osinfo, output, paths, secretportal
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$")
HEADER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_VALUE = 8192
MAX_TTL_S = 86400  # a per-secret lease limit above a day is a typo
WALL_USER = "ws-broker"
WALL_GROUP = "ws-clients"
WALL_HOME = "/var/lib/ws-broker"
WALL_SOCKET = "/run/ws-broker/broker.sock"
WALL_BIN = "/opt/ws/bin/ws"  # a root-owned launcher: the broker never runs code a normal user can write
WALL_LIB = "/opt/ws/lib"
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./+-]+$")
PATH_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
AREA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# A `base_path` may end in PATH_TOKEN, once: the broker puts the value there instead of in a header, for an API that
# takes its key in the path (Telegram: `/bot<token>/sendMessage`). Such a value must match PATH_VALUE_RE (no / . % ? #
# or space), so it can never change which path the request reaches.
PATH_TOKEN = "{value}"
PATH_VALUE_RE = re.compile(r"^[A-Za-z0-9_:-]{1,256}$")
# The tiers, lowest first. A run's tier reaches the names of the tiers in REACH; `walled` reaches only walled names, and
# nothing reaches up into it, so a walled class's keys never leave its own runs.
TIERS = ("walled", "worker", "lead", "lead-elevated")
REACH = {
    "walled": frozenset({"walled"}),
    "worker": frozenset({"worker"}),
    "lead": frozenset({"worker", "lead"}),
    "lead-elevated": frozenset({"worker", "lead", "lead-elevated"}),
}
ELEVATED = "lead-elevated"
DEFAULT_TIER = "worker"  # the tier of a name that has none, when a run asks for it
SOURCES = ("paste", "generate")
GENERATE_BYTES = 32  # 256 bits of randomness, URL-safe base64: 43 characters


class SecretsError(Exception):
    """Never carries a secret value."""


@dataclass(frozen=True)
class Entry:
    name: str
    hosts: tuple[str, ...] = ()
    header: str = "x-api-key"
    format: str = "{value}"
    purposes: tuple[str, ...] = field(default_factory=tuple)
    tier: str | None = None
    area: str | None = None
    kind: str = "header"  # header: the broker adds it to requests; else a minter in ws.minters makes a credential
    paths: tuple[str, ...] = ()  # request paths the broker forwards for this name (prefixes); empty: any path
    base_path: str = ""  # a path the broker puts in front of the request path on its way out (the client's path is still checked by `paths`)
    ttl: float | None = None  # longest lease of this name, seconds; None: the broker's own limit (one hour by default)
    options: object = None  # the minter's own keys, parsed by it
    source: str = "paste"  # paste: a person brings the value; generate: `ws secret generate` makes it

    @property
    def token_in_path(self) -> bool:
        """True when the value goes into the request path (base_path ends in {value}) and no header carries it."""
        return self.base_path.endswith(PATH_TOKEN)

    @property
    def scoped(self) -> bool:
        """True when only a run record that declares it may lease it."""
        return self.tier is not None or self.area is not None

    @property
    def elevated(self) -> bool:
        return self.tier == ELEVATED


def registry_path(env: dict | None = None) -> Path:
    return paths.config_dir(env) / "secrets.toml"


def _profile_secrets(env: dict | None) -> dict:
    try:
        prof = load(profile_path(env)) or {}
    except ProfileError as e:
        raise SecretsError(str(e)) from e
    roles = prof.get("roles")
    cfg = roles.get("secrets") if isinstance(roles, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def store_dir(env: dict | None = None, cfg: dict | None = None) -> Path:
    """`[roles.secrets] store` in the profile (or the `cfg` already read from it), else `secrets/` in the data folder."""
    s = (_profile_secrets(env) if cfg is None else cfg).get("store")
    if isinstance(s, str) and s:
        return Path(s).expanduser()
    return paths.data_dir(env) / "secrets"


def socket_path(env: dict | None = None, cfg: dict | None = None) -> Path:
    """The broker socket a client uses: $WS_BROKER_SOCKET, else `[roles.secrets] socket`, else `run/broker.sock` in the state folder."""
    env = os.environ if env is None else env
    if env.get("WS_BROKER_SOCKET"):
        return Path(env["WS_BROKER_SOCKET"]).expanduser()
    s = (_profile_secrets(env) if cfg is None else cfg).get("socket")
    if isinstance(s, str) and s:
        return Path(s).expanduser()
    return paths.state_dir(env) / "run" / "broker.sock"


class RegistryUntrusted(SecretsError):
    """The registry file could be changed by someone other than its reader: owned by another user, or group or world
    writable. The broker refuses it whole (code `registry_untrusted`) rather than trust the names and hosts in it."""

    kind = "untrusted"


def registry_problem(st: os.stat_result, owner_uid: int) -> str | None:
    """Why a registry with this stat cannot be trusted by `owner_uid`, or None. Root's file is trusted too."""
    if st.st_uid not in (owner_uid, 0):
        return f"it belongs to uid {st.st_uid}, not the broker's user or root"
    if st.st_mode & 0o022:
        return f"it can be written by group or others (mode {st.st_mode & 0o777:04o}; run chmod go-w on it)"
    return None


def _read_trusted(path: Path, owner_uid: int) -> str | None:
    """The registry's text as `owner_uid` may trust it, or None when there is no file. Raises RegistryUntrusted,
    OSError or UnicodeDecodeError."""
    try:
        pst = os.stat(path.parent)
    except FileNotFoundError:
        return None
    if pst.st_mode & 0o022:
        raise RegistryUntrusted(f"{path}: its folder can be written by group or others, so the file could be swapped")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RegistryUntrusted(f"{path}: it is a symlink; put the file itself there") from e
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise RegistryUntrusted(f"{path}: it is not a regular file")
        why = registry_problem(st, owner_uid)
        if why:
            raise RegistryUntrusted(f"{path}: {why}")
        return f.read().decode("utf-8")


def load_registry(path: Path, owner_uid: int | None = None) -> dict[str, Entry]:
    """The registry. A missing file is an empty registry; a file that does not parse, or a bad entry, raises.
    With `owner_uid` (the broker's), RegistryUntrusted for a symlink, anything but a regular file, a folder group or
    world can write, or a file owned by another user or open to group or world writes. The checks are on the opened
    file (no symlink followed, never blocking on a FIFO), so a swap between the check and the read cannot slip past."""
    if owner_uid is None and not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8") if owner_uid is None else _read_trusted(path, owner_uid)
        if text is None:
            return {}
        raw = tomllib.loads(text)
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


def _minters() -> dict:
    from ws.minters import github_app, imap, ssh_ca

    return {m.KIND: m for m in (github_app, imap, ssh_ca)}


def clean_value(entry: Entry, value: str) -> str:
    """A value as its kind wants it stored: a minter with a `clean_value(options, value)` tidies or refuses it (an
    imap entry for Gmail drops the spaces Google shows in an app password); any other kind is unchanged. Raises
    SecretsError, whose text never holds the value."""
    m = _minters().get(entry.kind)
    fn = getattr(m, "clean_value", None)
    return value if fn is None else fn(entry.options, value)


def _entry(path: Path, name: str, t: dict) -> Entry:
    kind = t.get("kind", "header")
    minters = _minters()
    if kind != "header" and kind not in minters:
        raise SecretsError(f"{path}: [{name}] kind must be header or one of {', '.join(sorted(minters))}")
    hosts = tuple(h.lower() for h in _str_list(path, name, "hosts", t.get("hosts", [])))
    for h in hosts:
        if not HOST_RE.match(h):
            raise SecretsError(f"{path}: [{name}] {h!r} is not a host name (no scheme, no path)")
    raw_base = t.get("base_path", "")
    token_in_path = isinstance(raw_base, str) and raw_base.endswith(PATH_TOKEN)
    if token_in_path and ("header" in t or "format" in t):
        raise SecretsError(
            f"{path}: [{name}] a base_path holding {{value}} sends no header: leave header and format out"
        )
    header = t.get("header", "" if token_in_path else "x-api-key")
    if not token_in_path and (not isinstance(header, str) or not HEADER_RE.match(header.lower())):
        raise SecretsError(f'{path}: [{name}] header must be a header name such as "x-api-key"')
    header = header.lower()
    fmt = t.get("format", "Bearer {value}" if header == "authorization" else "{value}")
    if not isinstance(fmt, str) or fmt.count("{value}") != 1 or re.search(r"[\x00-\x1f\x7f]", fmt):
        raise SecretsError(f"{path}: [{name}] format must contain {{value}} once, on one line")
    purposes = _str_list(path, name, "purposes", t.get("purposes", []))
    tier, area = t.get("tier"), t.get("area")
    if tier is not None and tier not in TIERS:
        raise SecretsError(f"{path}: [{name}] tier must be one of {', '.join(TIERS)}")
    if area is not None and not (isinstance(area, str) and AREA_RE.match(area)):
        raise SecretsError(f"{path}: [{name}] area must be a short lower-case name (letters, digits, - and _)")
    req_paths = _str_list(path, name, "paths", t.get("paths", []))
    if "paths" in t and not req_paths:
        raise SecretsError(
            f"{path}: [{name}] paths is empty: leave the key out to allow any path, or list the prefixes"
        )
    for pre in req_paths:
        if not PATH_PREFIX_RE.fullmatch(pre) or any(seg in (".", "..") for seg in pre.split("/")):
            raise SecretsError(
                f"{path}: [{name}] {pre!r} is not a path prefix (start with /; letters, digits and ._~/- only; no . or .. part)"
            )
    base_path = raw_base[: -len(PATH_TOKEN)] if token_in_path else raw_base  # {value} only once, only at the end
    if "base_path" in t and (
        not isinstance(base_path, str)
        or not PATH_PREFIX_RE.fullmatch(base_path)
        or "//" in base_path
        or any(seg in (".", "..") for seg in base_path.split("/"))
    ):
        raise SecretsError(
            f"{path}: [{name}] base_path must start with / and use letters, digits and ._~/- only: no . or .. part,"
            " no //, no query; {value} may stand once, at the end"
        )
    base_path = base_path + PATH_TOKEN if token_in_path else base_path.rstrip("/")
    ttl = t.get("ttl")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= MAX_TTL_S):
        raise SecretsError(f"{path}: [{name}] ttl must be a number of seconds above 0 and at most {MAX_TTL_S}")
    source = t.get("source", "paste")
    if source not in SOURCES:
        raise SecretsError(f"{path}: [{name}] source must be paste or generate")
    if source == "generate" and kind != "header":
        raise SecretsError(f"{path}: [{name}] a {kind} credential is brought by a person; source must be paste")
    allowed = {"hosts", "header", "format", "purposes", "tier", "area", "kind", "source", "ttl", "paths", "base_path"}
    options = None
    if kind != "header":
        allowed = {"tier", "area", "kind", "purposes", "source"} | minters[kind].EXTRA_KEYS
    unknown = set(t) - allowed
    if unknown:
        raise SecretsError(f"{path}: [{name}] has unknown keys: {', '.join(sorted(unknown))}")
    if kind != "header":
        options = minters[kind].parse(path, name, t)
    return Entry(
        name=name,
        hosts=hosts,
        header=header,
        format=fmt,
        purposes=purposes,
        tier=tier,
        area=area,
        kind=kind,
        paths=req_paths,
        base_path=base_path,
        ttl=float(ttl) if ttl is not None else None,
        options=options,
        source=source,
    )


# ---------------------------------------------------------------- the one registry writer (ADR 0034)
#
# ws appends an entry only on the person's yes (`ws mail connect`, `ws connect <flow>`) and takes out only a block it
# added. The file is never re-serialised, so comments and layout stay; the old file is backed up first; the new one is
# written to a temp file in the same folder and renamed over the old only if the old is still the file that was read.


class RegistryWriteError(SecretsError):
    """The registry could not be changed safely; nothing was changed. Never carries a value (the registry has none)."""


BACKUP_SUFFIX = ".bak"


def entry_block(name: str, table: dict, comment: str = "") -> str:
    """The TOML text of one header entry, from plain keys (lists of strings and strings only), checked by the same
    rules the broker loads it by. Raises SecretsError."""
    if not NAME_RE.match(name):
        raise SecretsError(f"{name!r} is not a valid name (letters, digits, - and _; starts with a letter)")
    lines = [f"# {comment}"] if comment else []
    lines.append(f"[{name}]")
    for k, v in table.items():
        if isinstance(v, str):
            lines.append(f"{k} = {_toml_str(v)}")
        elif isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
            lines.append(f"{k} = [{', '.join(_toml_str(x) for x in v)}]")
        else:
            raise SecretsError(f"[{name}] {k} must be a string or a list of strings")
    text = "\n".join(lines) + "\n"
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SecretsError(f"[{name}] does not make valid TOML ({e})") from e
    _entry(Path("the new entry"), name, raw[name])
    return text


def _toml_str(v: str) -> str:
    if re.search(r'[\x00-\x1f\x7f"\\]', v):
        raise SecretsError("a registry string may not hold a quote, a backslash or a control character")
    return f'"{v}"'


def entry_differences(entry: Entry, block: str) -> list[str]:
    """What differs between an entry already in the registry and the one `block` would add (empty: the same)."""
    want = _entry(Path("the new entry"), entry.name, tomllib.loads(block)[entry.name])
    out = []
    for key in ("kind", "hosts", "header", "format", "purposes", "paths", "base_path", "tier", "area", "ttl", "source"):
        a, b = getattr(entry, key), getattr(want, key)
        if isinstance(a, tuple):
            a, b = sorted(a), sorted(b)
        if a != b:
            out.append(f"{key}: the registry has {a!r}, this would add {b!r}")
    return out


def _open_registry(path: Path) -> tuple[bytes, tuple[int, int, int] | None]:
    """The registry's bytes and (device, inode, mode), or (b"", None) when there is none; refused as `_read_trusted`
    refuses: a symlink, not a regular file, another user's, or a file or folder group or others can write."""
    try:
        pst = os.stat(path.parent)
    except FileNotFoundError:
        return b"", None
    if pst.st_mode & 0o022:
        raise RegistryWriteError(
            f"{path.parent}: its folder can be written by group or others, so ws won't write there"
        )
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return b"", None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RegistryWriteError(f"{path}: it is a symlink; ws won't write through it") from e
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise RegistryWriteError(f"{path}: it is not a regular file")
        why = registry_problem(st, os.geteuid())
        if why:
            raise RegistryWriteError(f"{path}: {why}")
        return f.read(), (st.st_dev, st.st_ino, st.st_mode & 0o777)


def _write_beside(path: Path, data: bytes, mode: int) -> Path:
    """`data` in a new temp file next to `path` (O_EXCL, 0600, fsynced, then `mode`). Returns its path."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), mode)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return tmp


def _fsync_dir(folder: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def _registry_lock(path: Path):
    """An exclusive lock on `<registry>.lock`, held by every ws writer of the registry (no lock where there is no flock)."""
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _rewrite(path: Path, change, *, backup: bool = False) -> tuple[bool, bool]:
    """Replace the registry with `change(old_text)` (None: leave it), under the registry lock. With `backup`, the old
    file is copied to `<registry>.bak` first. The new text must load, and it is renamed in only if the file is still the
    one read (same inode and bytes). Returns (changed, backed up). Raises RegistryWriteError."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _open_registry(path)  # refuse a symlink or a loose folder before a lock file is made there
        with _registry_lock(path):
            return _rewrite_locked(path, change, backup)
    except RegistryWriteError:
        raise
    except (OSError, SecretsError) as e:
        raise RegistryWriteError(f"{path}: {e}") from e


def _rewrite_locked(path: Path, change, backup: bool) -> tuple[bool, bool]:
    old, ident = _open_registry(path)
    try:
        new_text = change(old.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise RegistryWriteError(f"{path}: it is not UTF-8 text") from e
    if new_text is None:
        return False, False
    mode = ident[2] if ident else 0o600
    backed_up = backup and ident is not None
    if backed_up:
        bak = _write_beside(path, old, 0o600)
        os.replace(bak, path.with_name(path.name + BACKUP_SUFFIX))
    tmp = _write_beside(path, new_text.encode("utf-8"), mode)
    try:
        load_registry(tmp)  # the whole new file loads, or nothing changes
        now, now_ident = _open_registry(path)
        same_file = (now_ident[:2] if now_ident else None) == (ident[:2] if ident else None)
        if not same_file or hashlib.sha256(now).digest() != hashlib.sha256(old).digest():
            raise RegistryWriteError(f"{path}: it changed while ws was writing it; nothing was changed, try again")
        os.rename(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(path.parent)
    return True, backed_up


def append_entry(path: Path, block: str) -> bool:
    """Add one new entry, as text, at the end of the registry (comments and layout kept), backing the old file up to
    `<registry>.bak`. Returns whether there was an old file to back up. Raises RegistryWriteError."""

    def change(old: str) -> str:
        sep = "" if not old or old.endswith("\n\n") else "\n" if old.endswith("\n") else "\n\n"
        return old + sep + block

    return _rewrite(path, change, backup=True)[1]


def remove_block(path: Path, block: str) -> bool:
    """Take out exactly `block` (the last copy), keeping everything else. False when it is not there unchanged."""

    def change(old: str) -> str | None:
        at = old.rfind(block)
        if at < 0:
            return None
        rest = old[at + len(block) :]
        nxt = next((ln.strip() for ln in rest.splitlines() if ln.strip() and not ln.strip().startswith("#")), "")
        if nxt and not nxt.startswith("["):
            return None  # a key was added under the block: it belongs to the entry, so the entry was changed
        return (old[:at].rstrip("\n") + "\n" if old[:at].strip() else "") + rest

    return _rewrite(path, change)[0]


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


def open_store(env: dict | None = None):
    """The store the broker reads: the folder in the data directory, or the OpenBao backend when the profile's secrets role is `openbao`."""
    cfg = _profile_secrets(env)
    if cfg.get("adapter") == "openbao":
        from ws.roles import secrets_openbao  # lazy: the role imports this module

        return secrets_openbao.store_from_cfg(cfg, env)
    return Store(store_dir(env, cfg))


def local_store(env: dict | None = None) -> Store:
    """The store that setting and the prompt write to. Not the OpenBao backend: its values are written in OpenBao."""
    if _profile_secrets(env).get("adapter") == "openbao":
        raise SecretsError("this profile keeps values in OpenBao: write them there (`bao kv put`), not with ws")
    return Store(store_dir(env))


def health(reg_path: Path, store: Store, owner_uid: int | None = None) -> tuple[list[str], int | None]:
    """What is wrong with the registry and the store, as plain sentences (never a value), and the store's owner uid.
    With `owner_uid`, a registry that user could not trust (load_registry) is a problem too.
    Raises PermissionError if the store belongs to another user: the caller then asks the broker."""
    problems = []
    if not reg_path.is_file():
        problems.append(f"no registry at {reg_path}")
    try:
        reg = load_registry(reg_path, owner_uid=owner_uid)
    except SecretsError as e:
        problems.append(f"registry unreadable: {e}")
        reg = {}
    problems += [
        f"{e.name} has no hosts, so it can never be used"
        for e in reg.values()
        if e.kind == "header" and not e.hosts and e.source != "generate"
    ]
    if getattr(store, "remote", False):  # a backend with no folder: its own checks, and the token file's owner
        if not any(p.startswith("registry unreadable") for p in problems):
            with contextlib.suppress(SecretsError):
                problems += [f"{n} is in the store but not in the registry" for n in unregistered(reg, store)]
        owner = None
        with contextlib.suppress(OSError):
            owner = store.path.stat().st_uid
        return [*problems, *store.problems()], owner
    try:
        owner = store.path.stat().st_uid
    except FileNotFoundError:
        return [*problems, f"no store at {store.path}"], None
    if not any(p.startswith("registry unreadable") for p in problems):
        problems += [f"{n} is in the store but not in the registry" for n in unregistered(reg, store)]
    return [*problems, *store.loose()], owner


def unregistered(reg: dict[str, Entry], store: Store) -> list[str]:
    """Names with a value in the store and no registry entry: a value nothing may lease, or a registry that lost a row.
    Raises PermissionError if the store belongs to another user."""
    return [n for n in store.names() if n not in reg]


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
        value = clean_value(reg[args.name], check_value(_read_value(args.name)))
        local_store().write(args.name, value)
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
        if len(v) >= 2 and v[0] in "\"'" and v[0] in v[1:]:
            q = v.index(v[0], 1)
            rest = v[q + 1 :].strip()
            if rest and not rest.startswith("#"):
                raise SecretsError(f"{path}: line {n} has text after the closing quote")
            v = v[1:q]
        elif " #" in v or "\t#" in v:
            v = re.split(r"\s#", v, maxsplit=1)[0].rstrip()
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
        local_store().write(args.name, value)
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
    except OSError as e:
        raise WsError(
            "failed",
            f"{args.name} could not be written to {store_dir()}: {e.strerror or type(e).__name__}",
            "check the store folder exists and is yours, then run this again",
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


AUDIT_LOG = "secrets-audit.jsonl"
AUDIT_SCHEMA = 1  # the `schema` of a line in logs/secrets-audit.jsonl


def _audit(action: str, name: str, result: str) -> None:
    """One line in `<state>/logs/secrets-audit.jsonl` per generate and reveal: who, which name, when, what came of it.
    Never a value, a link or a token. Best effort: a full disk does not undo a value that was stored."""
    import getpass as _getpass

    from ws import agentsession, jsonl

    try:
        who = _getpass.getuser()
    except (OSError, KeyError):
        who = "?"
    try:
        session = agentsession.refusal(os.environ, sys.stdin.isatty(), sys.stdout.isatty()) is not None
    except OSError:
        session = True
    rec = {
        "type": "ws.secret-audit/1",
        "action": action,
        "name": name,
        "result": result,
        "user": who,
        "in_session": session,
    }
    with contextlib.suppress(OSError):
        jsonl.append(paths.state_dir() / "logs" / AUDIT_LOG, jsonl.stamped(rec, AUDIT_SCHEMA))


def _registered(name: str) -> Entry:
    reg = load_registry(registry_path())
    if name not in reg:
        raise WsError(
            "not_found",
            f"{name!r} is not in {registry_path()}",
            f'add it first, e.g.\n    [{name}]\n    source = "generate"',
            2,
        )
    return reg[name]


def cmd_generate(args: argparse.Namespace) -> int:
    """Make a strong random value for a `source = "generate"` name and store it (atomic, mode 0600). Never printed."""
    import secrets as pysecrets

    from ws import reveal

    store = Store(store_dir())
    try:
        entry = _registered(args.name)
        if entry.source != "generate":
            raise WsError(
                "usage",
                f"{args.name} is a value a person brings (source = {entry.source!r}), so ws cannot make it",
                f'set it with `ws secret set {args.name}`, or mark it source = "generate" in {registry_path()}',
                2,
            )
        had = args.name in store.names()
        if had and not args.replace:
            raise WsError(
                "refused",
                f"{args.name} already has a value; a new one would lock out whatever uses the old one",
                f"`ws secret generate {args.name} --replace` makes a new one anyway",
            )
        store.write(args.name, pysecrets.token_urlsafe(GENERATE_BYTES))
        reveal.clear(args.name, reveal.marker_dir())
        _audit("generate", args.name, "replaced" if had else "made")
    except SecretsError as e:
        raise WsError("failed", str(e), "see `ws help secret`") from e
    except PermissionError as e:
        raise WsError(
            "refused",
            f"the store {store_dir()} belongs to another user",
            f"run it as that user: `{wall_hint('generate', args.name)}`",
        ) from e
    if args.json:
        output.result({"name": args.name, "generated": True, "replaced": had})
        return 0
    print(
        f"ws: generated {args.name} ({GENERATE_BYTES * 8} bits; {'replaced the old value; ' if had else ''}"
        f"never shown). To keep a copy: `ws secret reveal {args.name}`"
    )
    return 0


def _owner(args: argparse.Namespace) -> str | None:
    """--owner, else `owner` in [roles.secrets]: the tailnet login (or its start) whose device may open a page."""
    if args.owner:
        return args.owner
    try:
        o = _profile_secrets(None).get("owner")
    except SecretsError:
        return None
    return o if isinstance(o, str) and o else None


_NO_OWNER = (
    'set `owner` in [roles.secrets] of your profile to your tailnet login (or its start, such as "name@"), '
    "or pass --owner"
)


def _push_link(name: str, url: str, minutes: int) -> None:
    """Push the reveal link through the notify role, so the person taps it on their phone. A failed push is a warning:
    the link is still printed, and the page still serves its one use."""
    from ws.roles import commands, ntfy

    try:
        cfg = commands._role("notify", "ntfy")
        ntfy.send(
            cfg,
            os.environ,
            f"Tap to see {name} once, on this device, within {minutes} minutes. Have your password manager ready.",
            f"ws: {name}",
            "push",
            click=url,
        )
    except (ValueError, ntfy.NotifyError) as e:
        print(f"ws: the link was not pushed ({e}); open it from here instead", file=sys.stderr)
        return
    print("ws: pushed the link to your phone", file=sys.stderr)


def cmd_reveal(args: argparse.Namespace) -> int:
    """Serve the reveal-once page for a generated value (ws.reveal), print its link, wait for one use or expiry."""
    import ipaddress

    from ws import reveal, tailnet

    try:
        entry = _registered(args.name)
    except SecretsError as e:
        raise WsError("bad_config", str(e), f"fix {registry_path()}") from e
    if entry.source != "generate":
        raise WsError(
            "usage",
            f"{args.name} is a value a person brought, so they have it already; reveal shows only what ws generated",
            "",
            2,
        )
    owner = _owner(args)
    if args.bind:
        addr = args.bind.strip()
    elif args.allow_loopback:
        addr = "127.0.0.1"
    else:
        if owner is None:
            raise WsError("usage", "no owner is set, so no device would be let in", _NO_OWNER, 2)
        try:
            addr = tailnet.tailnet_ip()
        except tailnet.TailnetError as e:
            raise WsError("unavailable", str(e), "start tailscale, or name the address with --bind") from e
    try:
        loopback = ipaddress.ip_address(addr).is_loopback
    except ValueError:
        loopback = False
    if not loopback and owner is None:
        raise WsError("usage", "no owner is set, so no device would be let in", _NO_OWNER, 2)

    own = set() if loopback else {addr, *tailnet.own_addresses()}

    def allowed(ip: str) -> bool:
        if loopback:  # only with --allow-loopback (bind_ok refuses loopback otherwise): this machine's own clients
            try:
                return ipaddress.ip_address(ip).is_loopback
            except ValueError:
                return False
        if ip in own:  # a program on this machine is not the owner's device, whoever tailscale says it is
            return False
        return tailnet.whois_ok(ip, owner)

    try:
        page = reveal.make_page(
            args.name, addr, Store(store_dir()), allowed=allowed, allow_loopback=args.allow_loopback, again=args.again
        )
    except reveal.RevealError as e:
        raise WsError("failed", str(e)) from e
    host, port = page.server.server_address[:2]
    url = f"http://{f'[{host}]' if ':' in host else host}:{port}/{page.token}"
    minutes = max(1, min(args.minutes, reveal.LIFETIME_S // 60))
    print(url, file=sys.stderr if args.json else sys.stdout, flush=True)  # --json: stdout holds only the envelope
    if not args.json:
        print(
            f"ws: open that link on your own device within {minutes} minute{'' if minutes == 1 else 's'}; it shows {args.name} once",
            file=sys.stderr,
        )
    if args.notify:
        _push_link(args.name, url, minutes)
    _audit("reveal", args.name, "started")
    result = page.serve(minutes * 60)
    _audit("reveal", args.name, result)
    if args.json:
        output.result({"name": args.name, "url": url, "result": result})
    elif result == "revealed":
        print(f"ws: shown {args.name} once; the page is closed")
    else:
        print(
            f"ws: {args.name} was not shown ({result}); `ws secret reveal {args.name}` makes a new link",
            file=sys.stderr,
        )
    return 0 if result == "revealed" else 1


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
    p = ssub.add_parser("generate", help='make a strong random value for a source = "generate" name (never shown)')
    p.add_argument("name")
    p.add_argument("--replace", action="store_true", help="make a new value even if one is stored")
    p.set_defaults(func=cmd_generate)
    p = ssub.add_parser("reveal", help="show a generated value once, on your own device, through a one-time page")
    p.add_argument("name")
    p.add_argument("--owner", metavar="LOGIN", help="the tailnet login (or its start) whose device may open it")
    p.add_argument("--bind", metavar="ADDR", help="the one tailnet address to listen on (default: tailscale ip -4)")
    p.add_argument("--minutes", type=int, default=15, help="how long the link works, at most 15 (default 15)")
    p.add_argument("--again", action="store_true", help="show a value that was already shown once")
    p.add_argument("--notify", action="store_true", help="push the link to your phone ([roles.notify]); tap to open")
    p.add_argument("--allow-loopback", action="store_true", help=argparse.SUPPRESS)  # a box with no tailscale; tests
    p.set_defaults(func=cmd_reveal)
    p = ssub.add_parser("list", help="names, hosts, set/unset and owner; never values")
    p.set_defaults(func=cmd_list)
    p = ssub.add_parser("rm", help="remove a stored value")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)
    secretportal.register(ssub)
    p = ssub.add_parser("wall", help="print the one-time sudo steps that put the store behind its own system user")
    p.set_defaults(func=cmd_wall)
