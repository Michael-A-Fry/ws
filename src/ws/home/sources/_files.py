"""The only way a home source opens a file: a regular file, never a `.key`, bounded, and a failure is a SourceError.

A line or file that isn't a JSON object is not a record and is skipped, the way ws's own logs are read. A file that
can't be opened or listed is not a skipped record but a source that can't be read, so it raises.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ws.home.sources import UNREADABLE, SourceError

LINE_CAP = 1024 * 1024  # bytes of one JSON line that are looked at; a longer line is skipped
FILE_CAP = 64 * 1024 * 1024  # bytes of one file that are read
SMALL_CAP = 256 * 1024  # bytes of a one-record JSON file
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _refuse(path: Path) -> None:
    if path.name.endswith(".key"):
        raise SourceError(UNREADABLE, "a .key file is never opened")


def _open(path: Path):
    """The file open for binary reading, or None when it's missing or not a regular file. It's opened without blocking
    and checked after, so a pipe planted under a log's name is dropped, not waited on."""
    _refuse(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't read {path.name}: {e.strerror or type(e).__name__}") from e
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def iter_jsonl(path: Path, max_bytes: int = FILE_CAP, line_cap: int = LINE_CAP) -> Iterator[dict]:
    """The JSON objects of a JSON-lines file, in order. No file is none; a file over `max_bytes` raises."""
    f = _open(path)
    if f is None:
        return
    with f:
        if os.fstat(f.fileno()).st_size > max_bytes:
            raise SourceError(UNREADABLE, f"{path.name} is larger than {max_bytes // (1024 * 1024)} MiB")
        while True:
            line = f.readline(line_cap + 1)
            if not line:
                return
            if len(line) > line_cap and not line.endswith(b"\n"):
                while (rest := f.readline(line_cap)) and not rest.endswith(b"\n"):  # skip to the end of the line
                    pass
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                yield obj


def read_object(path: Path) -> dict | None:
    """One JSON object from a small file; None when there is no such file or it isn't an object."""
    f = _open(path)
    if f is None:
        return None
    with f:
        data = f.read(SMALL_CAP + 1)
    if len(data) > SMALL_CAP:
        return None
    try:
        obj = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def json_files(folder: Path) -> list[Path]:
    """The `*.json` files of a folder, sorted; no folder is none. The glob can't match a `.key` file."""
    try:
        return sorted(p for p in folder.glob("*.json") if not p.name.endswith(".key"))
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't list {folder.name}: {e.strerror or type(e).__name__}") from e


def day_files(folder: Path, since_epoch: float) -> list[Path]:
    """The `<date>.jsonl` files of a folder from the UTC day of `since_epoch` on, oldest first."""
    first = datetime.fromtimestamp(max(since_epoch, 0), UTC).strftime("%Y-%m-%d")
    try:
        names = sorted(p for p in folder.glob("*.jsonl") if _DAY.match(p.stem) and p.stem >= first)
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't list {folder.name}: {e.strerror or type(e).__name__}") from e
    return names


def iso(value) -> str | None:
    """A time as RFC 3339 UTC with a Z, from epoch seconds, epoch milliseconds or ISO text; None when it isn't one."""
    try:
        if isinstance(value, bool) or value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            t = datetime.fromtimestamp(seconds, UTC)
        else:
            t = datetime.fromisoformat(str(value))
            t = t if t.tzinfo else t.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(text: str | None) -> float:
    """Seconds since 1970 for a time `iso` made; 0 when there is none."""
    if not text:
        return 0.0
    return datetime.fromisoformat(text).timestamp()


def repo_label(cwd: str) -> str:
    """The last folder of a path: the label shown for a repo."""
    return os.path.basename(cwd.rstrip("/\\")) if cwd else ""


def text(value, limit: int = 200) -> str:
    """A field as plain one-line text, cut at `limit`; anything that isn't a string is empty."""
    if not isinstance(value, str):
        return ""
    one = " ".join(value.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"
