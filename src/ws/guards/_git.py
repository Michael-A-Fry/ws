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
    keep = {
        k: v for k, v in os.environ.items() if k not in drop and not k.startswith("GIT_CONFIG")
    }  # no config passed in by variable
    return {**keep, "GIT_NO_LAZY_FETCH": "1"}


def lines(folder: str, *args: str) -> list[str] | None:
    """The lines `git -C folder args` prints, or None when it fails, isn't there or takes too long."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", folder, *args],
            capture_output=True,
            text=True,
            timeout=SECONDS,
            env=env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def main_toplevel(folder: str) -> str | None:
    """The root of the main checkout `folder` is in, or None. Main means git's own folder is `<root>/.git` and is the common one, so a linked
    worktree is not, and neither is a repository whose `core.worktree` (or a rewritten `.git` file) points somewhere else, which a session
    can set in its own repository's config; and `folder` itself must lie inside the root git names."""
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return None
    got = lines(folder, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if got is None or len(got) != 3 or not all(os.path.isabs(x) for x in got):
        return None
    git_dir, common, top = (os.path.realpath(x) for x in got)
    here = os.path.realpath(folder)
    if git_dir != common or git_dir != os.path.join(top, ".git"):
        return None
    return top if here == top or here.startswith(top.rstrip("/") + "/") else None
