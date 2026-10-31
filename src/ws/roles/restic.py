"""Backup role, `restic`: your memory, notes and profile in an encrypted restic repository, with a read-back.

    [roles.backup]
    adapter = "restic"
    repo = "/mnt/backup/ws"                where the repository is (a path or any restic repository URL)
    password_secret = "restic-password"    the secret's name in ws's store (default), or
    password_file = "/path/to/file"        a file only you can read

`ws backup run` backs up the three; `ws backup verify` is the proof it can come back: it restores one random file from
the latest snapshot into a temporary folder and compares its SHA-256 with the live file. A file that changed since the
snapshot is skipped, not counted. `ws doctor` warns until a verify has passed in the last 30 days.

The password: the broker hands out credentials as request headers and has no way to give a program a raw value, and
restic needs the value itself. So this reads it in this process, from the store or the file, and hands it to restic
in its environment only; it is never an argument, never printed and never logged. That works while the store belongs to
you. If the store is walled to another user, use `password_file` (the wall protects API keys, not this).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import install, jobs
from ws import secrets as wsecrets
from ws.profile import ProfileError, load, profile_path, ws_home
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "backup", "restic"
VERIFY_FILE = "backup-verify.json"
VERIFY_MAX_AGE = timedelta(days=30)
DEFAULT_SECRET = "restic-password"
DEFAULT_SCHEDULE = "daily 03:00"
TAG = "ws"  # on every snapshot ws makes
LOOK_AT = 50  # files looked at for one that has not changed since the snapshot
MAX_VERIFY_BYTES = 50 * 1024 * 1024
BACKUP_TIMEOUT_S = 6 * 3600
PASS_ENV = ("PATH", "HOME", "LANG", "TZ", "TMPDIR")
_GLOB = re.compile(r"[*?\[\]\\]")
_USERINFO = re.compile(r"(?<=//)[^/@\s]*@")

Run = Callable[[list[str], dict[str, str], float], tuple[int, str, str]]


class BackupError(Exception):
    """A problem the person can fix; the message is safe to print (never the password)."""


@dataclass(frozen=True)
class Verified:
    ok: bool
    detail: str


def run_restic(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Run restic; (exit code, stdout, stderr). The default runner: tests pass their own and never call restic."""
    try:
        p = subprocess.run(
            ["restic", *argv], env=env, capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except FileNotFoundError as e:
        raise BackupError("restic is not on PATH") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise BackupError(f"restic did not run ({type(e).__name__})") from e
    return p.returncode, p.stdout, p.stderr


# ---- settings ------------------------------------------------------------------------------------------------------


def _profile(env) -> dict:
    try:
        return load(profile_path(env)) or {}
    except ProfileError as e:
        raise BackupError(str(e)) from e


def role_config(env) -> dict:
    roles = _profile(env).get("roles")
    cfg = roles.get("backup") if isinstance(roles, dict) else None
    if not isinstance(cfg, dict):
        raise BackupError('no [roles.backup] in the profile: add adapter = "restic" and repo = "..."')
    return cfg


def repo_of(cfg: dict) -> str:
    r = cfg.get("repo")
    if not isinstance(r, str) or not r.strip():
        raise BackupError("no repo set: where the restic repository is (a folder or a restic URL)")
    return r.strip()


def shown(repo: str) -> str:
    """The repository for the screen: a login in the URL is left out."""
    return _USERINFO.sub("", repo)


def sources(env) -> list[Path]:
    """What is backed up: the memory folder, the notes folder and the profile, whichever exist."""
    prof = _profile(env)
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}
    out = []
    for name in ("memory", "notes"):
        cfg = roles.get(name)
        p = cfg.get("path") if isinstance(cfg, dict) else None
        if isinstance(p, str) and p:
            out.append(install.expand(p, env))
    out.append(profile_path(env).absolute())
    return [p for p in dict.fromkeys(out) if p.exists()]


def _private_file(path: Path) -> None:
    """Raises BackupError unless `path` is a file of yours that nobody else can read or write."""
    try:
        st = path.stat()
    except OSError as e:
        raise BackupError(f"password_file {path}: {e.strerror or type(e).__name__}") from e
    if not stat.S_ISREG(st.st_mode):
        raise BackupError(f"password_file {path} is not a file")
    if st.st_uid != os.geteuid():
        raise BackupError(f"password_file {path} belongs to another user")
    if st.st_mode & 0o077:
        raise BackupError(
            f"password_file {path} is open to other users (mode {st.st_mode & 0o777:04o}); run `chmod 600 {path}`"
        )


def password(cfg: dict, env) -> str:
    """The repository password, from a file only you can read or from ws's store. Raises BackupError (no value in it)."""
    f = cfg.get("password_file")
    if f is not None:
        if not isinstance(f, str) or not f:
            raise BackupError("password_file must be a path")
        path = Path(f).expanduser()
        _private_file(path)
        try:
            return wsecrets.check_value(path.read_text(encoding="utf-8").rstrip("\r\n"))
        except (wsecrets.SecretsError, OSError, UnicodeDecodeError) as e:
            raise BackupError(f"password_file {path} cannot be used") from e
    name = cfg.get("password_secret", DEFAULT_SECRET)
    if not isinstance(name, str) or not wsecrets.NAME_RE.match(name):
        raise BackupError("password_secret must be the name of a secret (letters, digits, - and _)")
    store = wsecrets.Store(wsecrets.store_dir(env))
    try:
        return store.read(name)
    except FileNotFoundError as e:
        raise BackupError(f"{name} has no value; run `ws secret set {name}` (and add [{name}] to secrets.toml)") from e
    except wsecrets.SecretsError as e:
        if _walled(store.path):  # Store.read reports a closed store as SecretsError, never PermissionError
            raise BackupError(
                f"the store belongs to another user (or is closed to you), so restic cannot get {name}: "
                "set password_file to a file only you can read"
            ) from e
        raise BackupError(f"{name}: {e}") from e


def _walled(folder: Path) -> bool:
    try:
        return folder.stat().st_uid != os.geteuid() or not os.access(folder, os.R_OK | os.X_OK)
    except OSError:
        return True


def restic_env(cfg: dict, env) -> dict[str, str]:
    """What restic gets: a few plain variables and the repository and password. Nothing else of ws's."""
    out = {k: env[k] for k in PASS_ENV if env.get(k)}
    out["RESTIC_REPOSITORY"] = repo_of(cfg)
    out["RESTIC_PASSWORD"] = password(cfg, env)
    return out


def _latest() -> list[str]:
    """The snapshot to look at: the newest one ws made on this machine, not whatever else shares the repository."""
    return ["latest", "--tag", TAG, "--host", socket.gethostname()]


def _call(run: Run, argv: list[str], renv: dict[str, str], timeout: float) -> tuple[str, str]:
    code, out, err = run(argv, renv, timeout)
    if code:
        tail = (err or out).replace(renv["RESTIC_PASSWORD"], "***").strip().splitlines()[-1:]
        raise BackupError(f"restic {argv[0]} failed (exit {code}){': ' + tail[0][:200] if tail else ''}")
    return out, err


# ---- run, init, verify -----------------------------------------------------------------------------------------------


def init(cfg: dict, env, run: Run | None = None) -> str:
    _call(run or run_restic, ["init"], restic_env(cfg, env), 120)
    return f"made a restic repository at {shown(repo_of(cfg))}"


def backup(cfg: dict, env, run: Run | None = None) -> str:
    paths = sources(env)
    if not paths:
        raise BackupError("nothing to back up: no memory folder, notes folder or profile exists")
    _call(run or run_restic, ["backup", "--tag", TAG, *map(str, paths)], restic_env(cfg, env), BACKUP_TIMEOUT_S)
    return f"backed up {len(paths)} item{'' if len(paths) == 1 else 's'} to {shown(repo_of(cfg))}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _time(text: str) -> datetime | None:
    """restic's timestamps carry nanoseconds, which fromisoformat does not take: keep 6 digits."""
    try:
        t = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _listing(out: str) -> tuple[datetime | None, list[tuple[str, int]]]:
    """(snapshot time, [(path, size)] of the files) from `restic ls --json`, one JSON object a line."""
    when, files = None, []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("struct_type") == "snapshot" or ("hostname" in d and "time" in d):
            when = _time(d.get("time")) or when
        elif d.get("type") == "file" and isinstance(d.get("path"), str) and isinstance(d.get("size"), int):
            files.append((d["path"], d["size"]))
    return when, files


def verify(
    cfg: dict, env, run: Run | None = None, rng: random.Random | None = None, now: datetime | None = None
) -> Verified:
    """Restore random files from the latest snapshot until one can be compared with its live copy. Records the result."""
    rng, run = rng or random.SystemRandom(), run or run_restic
    renv = restic_env(cfg, env)
    out, _ = _call(run, ["ls", *_latest(), "--json"], renv, 600)
    taken, files = _listing(out)
    pool = [(p, n) for p, n in files if 0 < n <= MAX_VERIFY_BYTES and not _GLOB.search(p) and os.path.isabs(p)]
    rng.shuffle(pool)
    skipped = 0
    result = Verified(False, "no file in the latest snapshot could be compared with its live copy")
    for path, size in pool[:LOOK_AT]:
        live = Path(path)
        try:
            st = live.stat()
        except OSError:
            skipped += 1
            continue
        if not live.is_file() or st.st_size != size or (taken and datetime.fromtimestamp(st.st_mtime, UTC) > taken):
            skipped += 1  # changed since the snapshot: a mismatch would prove nothing
            continue
        tmp = tempfile.mkdtemp(prefix="ws-verify-")
        try:
            _call(run, ["restore", *_latest(), "--target", tmp, "--include", path], renv, 1800)
            restored = Path(tmp) / path.lstrip("/")
            if not restored.is_file():
                result = Verified(False, "restic restored nothing for the file it listed")
            elif _sha256(restored) != _sha256(live):
                result = Verified(False, f"restored copy of {live.name} differs from the file: the backup is not good")
            else:
                result = Verified(True, f"restored {live.name} ({size} bytes) and its SHA-256 matches")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        break
    else:
        if skipped:
            result = Verified(
                False,
                f"{skipped} files had changed since the snapshot and none could be compared: run `ws backup run`, then verify again",
            )
    record(env, result, now)
    return result


def verify_path(env) -> Path:
    return ws_home(env) / "logs" / VERIFY_FILE


def record(env, result: Verified, now: datetime | None = None) -> None:
    """Remember the result. The last pass stays on record when a later try fails: doctor judges by that date."""
    now = now or datetime.now(UTC)
    last_ok = now if result.ok else last_pass(env)
    doc = {"schema": 1, "checked_at": now.isoformat(), "ok": result.ok, "detail": result.detail}
    if last_ok:
        doc["last_ok_at"] = last_ok.isoformat()
    path = verify_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def last_pass(env) -> datetime | None:
    try:
        return _time(json.loads(verify_path(env).read_text(encoding="utf-8")).get("last_ok_at"))
    except (OSError, ValueError, AttributeError):
        return None


# ---- doctor and install --------------------------------------------------------------------------------------------


def check(cfg, osi, env, network):
    try:
        repo = repo_of(cfg)
    except BackupError as e:
        return FAIL, str(e)
    if not shutil.which("restic", path=env.get("PATH")):
        return FAIL, "restic is not on PATH"
    try:
        password(cfg, env)  # readable by the account the nightly job runs as; the value is dropped, never shown
    except BackupError as e:
        return FAIL, f"the nightly backup would fail: {e}"
    detail = f"restic repository {shown(repo)}"
    try:
        n = len(sources(env))
    except BackupError as e:
        return FAIL, str(e)
    if n == 0:
        return FAIL, f"{detail}; nothing to back up yet (no memory folder, notes folder or profile)"
    when = last_pass(env)
    now = datetime.now(UTC)
    if when is None:
        return WARN, f"{detail}; never verified: run `ws backup verify`"
    age = now - when
    if age > VERIFY_MAX_AGE:
        return WARN, f"{detail}; last verified {age.days} days ago: run `ws backup verify`"
    return OK, f"{detail}; {n} item{'' if n == 1 else 's'}; restore verified {age.days} days ago"


def steps(cfg, answers, env):
    """The backup job file. One you already have is kept as it is."""
    path = jobs.jobs_dir(env) / "backup.toml"
    schedule = cfg.get("schedule", DEFAULT_SCHEDULE)
    text = f'command = "backup"\nschedule = "{schedule}"\n'
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        jobs.parse_schedule(schedule if isinstance(schedule, str) else "", "[roles.backup] schedule")
    return [install.Step(path, "file", text, "your backup job (what the timer runs)", toml=True)]
