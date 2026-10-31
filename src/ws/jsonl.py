"""Bounded JSONL logs: one line per record, rotated at a size, with any one string field capped.

`runs.jsonl`, `ratings.jsonl` and `canary.jsonl` all go through here, so a runaway job cannot grow them without limit
(claw-code's session log does the same: three old files, a 16 KiB field cap).

  * Cap: a string longer than `field_cap` characters (default 16 KiB) is cut there and ends with
    `[truncated: N chars]`, N being the original length. It applies to every string value at any depth, not to keys.
  * Rotation: before a write that would take the live file past `max_bytes` (default 5 MB), the live file becomes
    `name.jsonl.1`, the old `.1` becomes `.2`, and so on. Up to `keep` (default 3) old files are kept. The `name.jsonl.N`
    form is logrotate's, and it keeps `*.jsonl` meaning "the live logs". A file past `keep` is dropped, unless
    `keep_if(record)` is true for any record in it (a run record from this month): spend is never undercounted.
  * Old files and the lock file can sit in `side_dir` instead of beside the log, for a log whose own folder an agent may
    write to (`ratings.jsonl` keeps them under `logs/`, which the gate denies a session).
  * Safe with two writers: every append, and the rotation it may cause, holds an exclusive flock on `name.jsonl.lock`,
    a file that is never renamed (an flock on the log itself would follow the inode through a rotation). A reader takes
    the shared lock, so it never reads while the files are being renamed. A lock held for `LOCK_TIMEOUT_S` (10 s)
    raises `LockTimeout`. Files and folders are 0600 and 0700, and a log or lock that is a symlink is refused
    (`O_NOFOLLOW`), so a planted link cannot point a write elsewhere.

`read` gives every record oldest first across the old files, skipping any line that is not a JSON object.

A session can read `logs/` (it may read its own run record; the gate denies it writes only) and any process it starts
can take a lock on a file it can open. The gate cannot hide the lock file: `deny_read` is a list of globs the person
sets, the matcher only sees commands, and the sandbox hides only paths that exist when it starts, so none of them stops
a program the session starts from opening a file. So a lock held to stall the log is assumed, and made harmless:
`probe` (the check before a run) takes the exclusive lock, so a held lock refuses the run rather than letting it go
unrecorded; and `append_plain` writes a record that could not take the lock to a sibling file that is never locked
(`runrecord` uses it for `runs.fallback.jsonl`), in one O_APPEND write, so nothing a session holds can keep a record out.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

try:  # not on native Windows; ws runs there only through WSL2, but the module must still import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 3
DEFAULT_FIELD_CAP = 16 * 1024
LOCK_TIMEOUT_S = 10.0  # how long a write or read waits for the lock before it gives up
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # a log that is a symlink is never followed (not on native Windows)


class LockTimeout(TimeoutError):
    """The log's lock was held for the whole wait: another process is stuck holding it. An OSError (TimeoutError is
    one), so a caller that already handles an unwritable log handles this too."""


def cap_fields(value, cap: int | None = None):
    """`value` with every string longer than `cap` cut and marked; dicts and lists are copied, not changed."""
    cap = DEFAULT_FIELD_CAP if cap is None else cap
    if isinstance(value, str):
        return value if len(value) <= cap else f"{value[:cap]}[truncated: {len(value)} chars]"
    if isinstance(value, dict):
        return {k: cap_fields(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [cap_fields(v, cap) for v in value]
    return value


def _side(path: Path, side_dir: Path | None) -> Path:
    return side_dir if side_dir is not None else path.parent


def archive_path(path: Path, n: int, side_dir: Path | None = None) -> Path:
    return _side(path, side_dir) / f"{path.name}.{n}"


def archives(path: Path, side_dir: Path | None = None) -> list[tuple[int, Path]]:
    """The old files as (number, path), newest (lowest number) first."""
    side = _side(path, side_dir)
    pat = re.compile(re.escape(path.name) + r"\.(\d+)$")
    try:
        names = os.listdir(side)
    except OSError:
        return []
    found = [(int(m[1]), side / n) for n in names if (m := pat.match(n))]
    return sorted(found)


def files(path: Path, side_dir: Path | None = None) -> list[Path]:
    """Every file of the log that exists, oldest first, the live file last."""
    old = [p for _, p in reversed(archives(path, side_dir))]
    return [*old, path] if os.path.lexists(path) else old


def _flock(fd: int, shared: bool, timeout_s: float, lock: Path) -> None:
    """Take the lock, trying without blocking until `timeout_s` has gone."""
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, mode)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout(f"{lock.name} has been locked for {timeout_s:g} s by another process") from None
            time.sleep(0.01)


@contextlib.contextmanager
def _locked(
    path: Path, side_dir: Path | None, shared: bool = False, create: bool = True, timeout_s: float | None = None
) -> Iterator[None]:
    """Hold the log's lock. A reader that cannot open the lock file (a read-only folder) goes without it."""
    lock = _side(path, side_dir) / f"{path.name}.lock"
    fd = None
    try:
        fd = os.open(lock, os.O_RDWR | NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    except OSError:
        if create:
            raise
    try:
        if fd is not None and fcntl is not None:
            _flock(fd, shared, LOCK_TIMEOUT_S if timeout_s is None else timeout_s, lock)
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing also drops the lock


def _open_text(path: Path):
    """The file for reading, refusing a symlink."""
    return os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW), encoding="utf-8", errors="replace")


def _parse(fh) -> Iterator[dict]:
    for line in fh:
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict):
                yield rec


def _has(path: Path, keep_if: Callable[[dict], bool]) -> bool:
    try:
        with _open_text(path) as fh:
            return any(keep_if(r) for r in _parse(fh))
    except OSError:
        return True  # cannot tell, so do not delete it


def rotate(path: Path, keep: int, side_dir: Path | None = None, keep_if: Callable[[dict], bool] | None = None) -> None:
    """Make the live file `.1` and shift the old ones up. The caller holds the lock."""
    old = [p for _, p in archives(path, side_dir)]
    kept = old[: max(keep - 1, 0)]
    for p in old[len(kept) :]:
        if keep_if is not None and _has(p, keep_if):
            kept.append(p)
        else:
            p.unlink(missing_ok=True)
    for i, p in enumerate(kept, 1):  # close any gap first (ascending: each target is free)
        if p != archive_path(path, i, side_dir):
            os.replace(p, archive_path(path, i, side_dir))
    for i in range(len(kept), 0, -1):  # then shift up from the top, so nothing is overwritten
        os.replace(archive_path(path, i, side_dir), archive_path(path, i + 1, side_dir))
    os.replace(path, archive_path(path, 1, side_dir))


def append(
    path: Path,
    rec: dict,
    *,
    max_bytes: int | None = None,
    keep: int | None = None,
    field_cap: int | None = None,
    side_dir: Path | None = None,
    keep_if: Callable[[dict], bool] | None = None,
    lock_timeout_s: float | None = None,
) -> None:
    """Add one record: fields capped, the file rotated first if the line would take it past `max_bytes`, then one
    write() on an O_APPEND fd. A line bigger than `max_bytes` still goes in, alone in a fresh file."""
    max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    keep = DEFAULT_KEEP if keep is None else keep
    field_cap = DEFAULT_FIELD_CAP if field_cap is None else field_cap
    line = (json.dumps(cap_fields(rec, field_cap), ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if side_dir is not None:
        side_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(path, side_dir, timeout_s=lock_timeout_s):
        if 0 < _size(path) and _size(path) + len(line) > max_bytes:
            rotate(path, keep, side_dir, keep_if)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if st.st_mode & 0o077 and st.st_uid == os.geteuid():  # made earlier with a wider mode
                os.fchmod(fd, 0o600)
            os.write(fd, line)
        finally:
            os.close(fd)


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except FileNotFoundError:
        return 0


def read(path: Path, side_dir: Path | None = None, lock_timeout_s: float | None = None) -> list[dict]:
    """Every record, oldest first, across the old files. A line that is not a JSON object is skipped, so one bad write
    never hides the rest. No files is no records; one that exists but cannot be read raises OSError."""
    out: list[dict] = []
    with _locked(path, side_dir, shared=True, create=False, timeout_s=lock_timeout_s):
        for p in files(path, side_dir):
            try:
                with _open_text(p) as fh:
                    out.extend(_parse(fh))
            except FileNotFoundError:
                continue
    return out


def probe(path: Path, side_dir: Path | None = None, lock_timeout_s: float | None = None) -> None:
    """Take the exclusive lock and let it go, to learn that a write would not wait. Raises LockTimeout if another
    process holds the lock (shared or exclusive) for the whole wait. Writes nothing: no lock file yet is no holder."""
    with _locked(path, side_dir, create=False, timeout_s=lock_timeout_s):
        pass


def append_plain(path: Path, rec: dict, field_cap: int | None = None) -> None:
    """One record, no lock and no rotation: field cap, then a single write() on an O_APPEND fd (0600, no symlink). For
    the fallback a record goes to when the locked log cannot be written; nothing can hold a lock on it."""
    cap = DEFAULT_FIELD_CAP if field_cap is None else field_cap
    line = (json.dumps(cap_fields(rec, cap), ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if st.st_mode & 0o077 and st.st_uid == os.geteuid():
            os.fchmod(fd, 0o600)
        os.write(fd, line)
    finally:
        os.close(fd)


def read_plain(path: Path) -> list[dict]:
    """Every record of one file, with no lock (the file `append_plain` writes). A missing file is no records."""
    try:
        with _open_text(path) as fh:
            return list(_parse(fh))
    except (FileNotFoundError, NotADirectoryError):
        return []
