"""A job's log file, kept small: at most `KEEP` files of `MAX_BYTES`, and nothing below INFO in the old ones.

`ws run` calls `rotate` when a scheduled run ends. A file over the cap becomes `<name>.log.1` (older ones shift up, the
oldest goes), with DEBUG and TRACE lines dropped on the way: the floor is INFO, so a chatty job cannot fill the disk
with what nobody reads. The archives keep INFO and above; the live file is whole until the next rotation.

Swap note: this is a plain rename chain; logrotate or journald limits can replace it, and the unit's `log = "journal"`
already does.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
KEEP = 3
_BELOW_INFO = re.compile(rb"^\s*(?:\[?\s*)?(?:DEBUG|TRACE)\b", re.IGNORECASE)


def above_floor(line: bytes) -> bool:
    """True for a line at INFO or above (a line that names no level is kept)."""
    return not _BELOW_INFO.match(line)


def rotate(path: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> bool:
    """Rotate `path` if it is over `max_bytes`. True when it rotated."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    oldest = path.with_name(f"{path.name}.{keep}")
    with contextlib.suppress(OSError):
        oldest.unlink()
    for i in range(keep - 1, 0, -1):
        with contextlib.suppress(OSError):
            os.replace(path.with_name(f"{path.name}.{i}"), path.with_name(f"{path.name}.{i + 1}"))
    first = path.with_name(f"{path.name}.1")
    try:
        with path.open("rb") as src, first.open("wb") as dst:
            for line in src:
                if above_floor(line):
                    dst.write(line)
        path.write_bytes(b"")  # truncate in place: a unit that still holds the file open keeps a valid descriptor
    except OSError:
        return False
    return True
