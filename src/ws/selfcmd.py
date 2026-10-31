"""How ws starts itself with this Python: `python -I -m ws`, so a folder named `ws/` in the working directory is never run.

Plain `python -m ws` puts the current folder first on `sys.path`. A session works in a project it does not control, so a
planted `ws/__main__.py` there would run in place of the gate hook, the broker or a timer's job. `-I` (isolated) drops the
current folder, the user site and the `PYTHON*` variables; the installed ws (a pipx environment, or an editable `uv run`
install, which is a `.pth` file in site-packages) is still found.

Every place ws spawns itself, or writes its own command into a runner's config, uses `argv()`. A command that is
already an absolute path to the `ws` script (the installer's way) needs none of this.
"""

from __future__ import annotations

import sys


def argv() -> list[str]:
    """The argv prefix that runs ws: this Python, isolated, `-m ws`. Add the subcommand after it."""
    return [sys.executable, "-I", "-m", "ws"]
