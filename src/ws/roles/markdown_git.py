"""Memory role, `markdown-git`: Markdown files with front matter in a git repo, searchable through a rebuildable index."""

from __future__ import annotations

from pathlib import Path

from ws import install, memory
from ws.roles import FAIL, OK, WARN, folder

ROLE, NAME = "memory", "markdown-git"


def check(cfg, osi, env, network):
    d, bad = folder(cfg)
    if bad:
        p = cfg.get("path")
        missing = isinstance(p, str) and p and not Path(p).expanduser().exists()
        return (bad[0], f"{bad[1]}: `ws memory init` makes it") if missing else bad
    if not memory.is_repo(d):
        return WARN, f"{d} is not in git: run `ws memory init`"
    built, detail = memory.check_index(d)
    return (OK, f"{d}: git repo; {detail}") if built else (FAIL, f"{d}: {detail}")


def steps(cfg, answers, env):
    """The memory folder. `ws memory init` makes it a git repo: that is a command, not a file ws can describe."""
    return [
        install.Step(
            install.expand(answers.memory or install.DEFAULT_MEMORY, env), "dir", None, "your memory folder", root=True
        )
    ]
