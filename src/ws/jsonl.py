"""Bounded, locked JSON-lines logs: one object per line, a size limit with rotation, and no lost lines.

Every ws log that grows over time (runs, ratings, canary, eval results, the sandbox wall report) is written through
here, and `stamped` gives each line its `schema` and `ts`; docs/reference/stores.md lists the logs. A long string field is cut and marked, a full file rotates into `name.N` (newest is `.1`), a reader spans the live
file and every old one, and writers take an advisory lock so two of them never interleave or lose a line. Nothing is
written through a symlink, and every file is private to its owner (0600 in a 0700 folder).

The lock lives in its own file (`name.lock`, never rotated) because locking the log itself would follow the inode
through a rename. A session under policy may be able to open that file, so a lock held for the whole wait is treated as
hostile: `probe` lets a caller refuse work up front, and `append_plain` / `read_plain` give a lock-free fallback file so
a stalled lock cannot hide a record. Where `fcntl` does not exist (native Windows) locking is skipped and the rest works.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX only; absent on native Windows, where locking is simply skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 3
DEFAULT_FIELD_CAP = 16 * 1024
LOCK_TIMEOUT_S = 10.0
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_POLL_S = 0.01
_NUMBER = re.compile(r"[0-9]+")


class LockTimeout(TimeoutError):
    """The lock stayed held by another process for the whole wait. An `OSError`, so "log not writable" handlers catch it."""


# ---------------------------------------------------------------------------------------------------------------------
# The stamp every line carries


def iso_utc(epoch: float) -> str:
    """RFC 3339 in UTC with a `Z`, to the millisecond: `2026-11-02T01:02:03.456Z`."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stamped(rec: dict, schema: int, epoch: float | None = None) -> dict:
    """`rec` behind the two keys every log line carries: `schema` (a whole number, one per log) and `ts` (when the line
    was written, `iso_utc`). A log that used `at` keeps writing it in `rec` beside these (see docs/reference/stores.md)."""
    return {"schema": schema, "ts": iso_utc(time.time() if epoch is None else epoch), **rec}


def check_stamp(rec: Any, time_keys: tuple[str, ...] = ("ts",)) -> None:
    """Raise ValueError unless `rec` is an object with a whole-number `schema` and a text value under one of `time_keys`
    (`ts`; a log whose schema fixes its own time keys, like the run record, names them). Every line a ws log takes
    passes this first, so a writer that forgets the stamp fails its own tests."""
    ok = (
        isinstance(rec, dict)
        and isinstance(rec.get("schema"), int)
        and not isinstance(rec.get("schema"), bool)
        and any(isinstance(rec.get(k), str) and rec[k] for k in time_keys)
    )
    if not ok:
        raise ValueError(f"a log line needs an integer schema and a {' or '.join(time_keys)} (see jsonl.stamped)")


def when(rec: dict) -> str | None:
    """When a line was written: its `ts`, else the legacy `at` of a line written before `ts` existed. None if neither is
    text. A line with no `schema` is schema 1."""
    for key in ("ts", "at"):
        value = rec.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Field cap and file naming


def cap_fields(value: Any, cap: int | None = None) -> Any:
    """A copy of `value` with every string longer than `cap` characters cut and marked with its original length."""
    limit = DEFAULT_FIELD_CAP if cap is None else cap
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}[truncated: {len(value)} chars]"
    if isinstance(value, dict):
        return {k: cap_fields(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [cap_fields(v, limit) for v in value]
    return value


def _folder(path: Path, side_dir: Path | None) -> Path:
    return Path(side_dir) if side_dir is not None else path.parent


def archive_path(path: Path, n: int, side_dir: Path | None = None) -> Path:
    """Where old file number `n` of this log lives."""
    return _folder(path, side_dir) / f"{path.name}.{n}"


def archives(path: Path, side_dir: Path | None = None) -> list[tuple[int, Path]]:
    """The old files that exist as `(number, path)`, newest first (`.1` is the most recent)."""
    folder = _folder(path, side_dir)
    prefix = path.name + "."
    found: list[tuple[int, Path]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if name.startswith(prefix) and _NUMBER.fullmatch(name[len(prefix) :]):
            found.append((int(name[len(prefix) :]), folder / name))
    return sorted(found)


def files(path: Path, side_dir: Path | None = None) -> list[Path]:
    """Every file of the log that exists, oldest first, with the live file last."""
    out = [p for _, p in reversed(archives(path, side_dir))]
    if os.path.lexists(path):
        out.append(path)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Lock


def _lock_path(path: Path, side_dir: Path | None) -> Path:
    return _folder(path, side_dir) / f"{path.name}.lock"


@contextmanager
def _locked(lock_file: Path, *, exclusive: bool, create: bool, timeout: float) -> Iterator[None]:
    """Hold the advisory lock on `lock_file` for the block.

    Writers pass `create=True` and a missing lock file is made (a symlink there is an error). Readers and `probe` pass
    `create=False`: if the file cannot be opened they carry on unlocked, because no file means no writer has run."""
    if fcntl is None:
        yield
        return
    flags = os.O_RDWR | NOFOLLOW | (os.O_CREAT if create else 0)
    try:
        fd = os.open(lock_file, flags, 0o600)
    except OSError:
        if create:
            raise
        yield
        return
    try:
        deadline = time.monotonic() + timeout
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{lock_file.name} stayed locked for {timeout:g}s") from None
                time.sleep(_POLL_S)
        yield
    finally:
        os.close(fd)  # closing the descriptor drops the lock


def _timeout(value: float | None) -> float:
    return LOCK_TIMEOUT_S if value is None else value


# ---------------------------------------------------------------------------------------------------------------------
# Reading


def _parse(data: bytes) -> list[dict]:
    out: list[dict] = []
    for line in data.decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_file(path: Path) -> list[dict]:
    """The records of one file. A symlink (or anything else that cannot be opened) raises `OSError`."""
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        return _parse(f.read())


def read(path: Path, side_dir: Path | None = None, lock_timeout_s: float | None = None) -> list[dict]:
    """Every record, oldest first: old files from the highest number down, then the live file. Bad lines are skipped."""
    path = Path(path)
    out: list[dict] = []
    with _locked(_lock_path(path, side_dir), exclusive=False, create=False, timeout=_timeout(lock_timeout_s)):
        for f in files(path, side_dir):
            try:
                out.extend(_read_file(f))
            except FileNotFoundError:
                continue
    return out


def read_plain(path: Path) -> list[dict]:
    """The records of one file, with no lock and no old files. Missing gives `[]`; a symlink raises `OSError`."""
    try:
        return _read_file(Path(path))
    except (FileNotFoundError, NotADirectoryError):
        return []


def probe(path: Path, side_dir: Path | None = None, lock_timeout_s: float | None = None) -> None:
    """Take the exclusive lock and let go at once, writing nothing. `LockTimeout` if someone else holds it."""
    with _locked(_lock_path(Path(path), side_dir), exclusive=True, create=False, timeout=_timeout(lock_timeout_s)):
        pass


# ---------------------------------------------------------------------------------------------------------------------
# Writing


def _make_dir(folder: Path) -> None:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)


def _encode(rec: Any, field_cap: int | None) -> bytes:
    text = json.dumps(cap_fields(rec, field_cap), separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("ascii")


def _write_line(path: Path, line: bytes) -> None:
    """Append one line in a single write; never through a symlink; narrow a wider file that we own."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        geteuid = getattr(os, "geteuid", None)
        if st.st_mode & 0o077 and geteuid is not None and st.st_uid == geteuid():
            os.fchmod(fd, 0o600)
        os.write(fd, line)
    finally:
        os.close(fd)


def _protected(path: Path, keep_if: Callable[[dict], bool]) -> bool:
    try:
        return any(keep_if(r) for r in _read_file(path))
    except OSError:
        return True  # unsure: do not delete


def _forget(path: Path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def rotate(path: Path, keep: int, side_dir: Path | None = None, keep_if: Callable[[dict], bool] | None = None) -> None:
    """Make the live file `.1` and shift the old ones up. The caller holds the exclusive lock.

    The newest `keep - 1` old files stay. Older ones go, unless `keep_if` is true for a record in them (or they cannot
    be read), so a log can hold more than `keep` old files. Gaps in the numbers close. Nothing is ever overwritten."""
    old = archives(path, side_dir)
    room = max(keep - 1, 0)
    kept = list(old[:room])
    for n, f in old[room:]:
        if keep_if is not None and _protected(f, keep_if):
            kept.append((n, f))
        else:
            _forget(f)
    for new_n, (n, f) in enumerate(kept, start=1):  # ascending, so a target number is always free
        if n != new_n:
            os.replace(f, archive_path(path, new_n, side_dir))
    for new_n in range(len(kept), 0, -1):  # descending, so a target number is always free
        os.replace(archive_path(path, new_n, side_dir), archive_path(path, new_n + 1, side_dir))
    try:
        os.replace(path, archive_path(path, 1, side_dir))
    except FileNotFoundError:
        pass


def append(
    path: Path,
    rec: Any,
    *,
    max_bytes: int | None = None,
    keep: int | None = None,
    field_cap: int | None = None,
    side_dir: Path | None = None,
    keep_if: Callable[[dict], bool] | None = None,
    lock_timeout_s: float | None = None,
    time_keys: tuple[str, ...] = ("ts",),
) -> None:
    """Add one record: check its stamp, cap its fields, take the exclusive lock, rotate if it would overflow, write the
    line.

    The record needs an integer `schema` and a `ts` (or one of `time_keys`): `check_stamp`. A line bigger than
    `max_bytes` still goes in, alone. Raises `LockTimeout` if the lock stays held, and `OSError` if the log or the lock
    file is a symlink or cannot be written."""
    check_stamp(rec, time_keys)
    path = Path(path)
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    line = _encode(rec, field_cap)
    _make_dir(path.parent)
    if side_dir is not None:
        _make_dir(Path(side_dir))
    with _locked(_lock_path(path, side_dir), exclusive=True, create=True, timeout=_timeout(lock_timeout_s)):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISREG(st.st_mode) and st.st_size > 0 and st.st_size + len(line) > limit:
            rotate(path, DEFAULT_KEEP if keep is None else keep, side_dir, keep_if)
        _write_line(path, line)


def append_plain(path: Path, rec: Any, field_cap: int | None = None, time_keys: tuple[str, ...] = ("ts",)) -> None:
    """Add one record to a single file with no lock and no rotation: the fallback when the locked log is unusable. The
    same stamp check as `append`."""
    check_stamp(rec, time_keys)
    path = Path(path)
    line = _encode(rec, field_cap)
    _make_dir(path.parent)
    _write_line(path, line)
