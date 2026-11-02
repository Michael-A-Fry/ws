"""A tmux target, checked: what `tmux ... -t T` is given is a name ws recognises, never an option or a shell word."""

from __future__ import annotations

import re

TARGET = re.compile(r"[A-Za-z0-9_.:@%$+=,/-]{1,128}")


def valid(target: str) -> bool:
    return bool(TARGET.fullmatch(target)) and not target.startswith("-")
