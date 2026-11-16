"""Asking git about a folder, for the guards that need to know which repository a session is in.

One place for it so each guard asks the same way: git runs with the variables that point it at a repository (`GIT_DIR` and its
kin) removed, so a folder is judged as git itself finds it from there; lazy fetching is off, so nothing goes to the network; and the
answer is None when git isn't there, fails or takes more than a few seconds. A guard treats None as "can't tell", which for a
guard that refuses is a refusal.
"""

from __future__ import annotations

import os
import subprocess

SECONDS = 5


def env() -> dict[str, str]:
    drop = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
    return {**{k: v for k, v in os.environ.items() if k not in drop}, "GIT_NO_LAZY_FETCH": "1"}


def lines(folder: str, *args: str) -> list[str] | None:
    """The lines `git -C folder args` prints, or None when it fails, isn't there or takes too long."""
    try:
        done = subprocess.run(
            ["git", "-C", folder, *args], capture_output=True, text=True, timeout=SECONDS, env=env(), check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def toplevel(folder: str) -> str | None:
    """The root of the working tree `folder` is in, as git says (a linked worktree has a root of its own), or None."""
    got = lines(folder, "rev-parse", "--show-toplevel")
    return got[0] if got and len(got) == 1 and os.path.isabs(got[0]) else None
