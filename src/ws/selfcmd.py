"""How ws starts itself with this Python: `python -I -m ws`, so a folder named `ws/` in the working directory is never run.

Plain `python -m ws` puts the current folder first on `sys.path`. A session works in a project it does not control, so a
planted `ws/__main__.py` there would run in place of the gate hook, the broker or a timer's job. `-I` (isolated) drops the
current folder, the user site and the `PYTHON*` variables; the installed ws (a pipx environment, or an editable `uv run`
install, which is a `.pth` file in site-packages) is still found.

Every place ws spawns itself, or writes its own command into a runner's config, uses `argv()`. A command that is
already an absolute path to the `ws` script (the installer's way) needs none of this.
"""

from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path


def argv() -> list[str]:
    """The argv prefix that runs ws: this Python, isolated, `-m ws`. Add the subcommand after it."""
    return [sys.executable, "-I", "-m", "ws"]


_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/folders", "/dev/shm")


def tree() -> Path | None:
    """The environment folder this Python lives in (a pipx, uv tool or venv folder), or None for a system Python. The
    gate hook runs code from here, so a gated session must not edit it."""
    if sys.prefix == sys.base_prefix:
        return None
    return Path(os.path.abspath(sys.prefix))


def unstable(cmd: list[str] | None = None) -> str | None:
    """Why this interpreter is a poor home for a hook that stays in a config file for months, or None when it is fine:
    a project's `.venv`, a Claude Code worktree, or a temp folder outside HOME all come and go. The path is read as written, not
    resolved: a venv's `python` is a link to the system one, and its own folder is what matters."""
    exe = (cmd or argv())[0]
    path = os.path.abspath(exe)
    parts = Path(path).parts
    if ".venv" in parts:
        return f"{path} is inside a project's .venv folder"
    if any(a == ".claude" and b == "worktrees" for a, b in itertools.pairwise(parts)):
        return f"{path} is inside a Claude Code worktree"
    home = os.path.abspath(os.path.expanduser("~"))
    if path.startswith(home + os.sep):
        return None  # under the person's own HOME (a uv or pipx tool install), wherever that HOME lives
    temp = {os.path.abspath(t) for t in (*_TEMP_ROOTS, tempfile.gettempdir())}
    if any(path == t or path.startswith(t + os.sep) for t in temp):
        return f"{path} is in a temp folder"
    return None
