"""guard-launch: an isolated agent or a workflow starts only from the main checkout of a git repository.

The folder the session is in decides where an isolated agent's worktree, and a workflow's fixes, land. Started from a
folder with no repository, or from a linked worktree (a worktree inside a worktree), the work lands somewhere nobody
looks. So a Workflow call, and an Agent or Task call that asks for `isolation: "worktree"`, is denied unless the
session's folder is inside a repository's main checkout: the first folder going up with a `.git` entry must hold a
`.git` folder (a linked worktree's `.git` is a file) and must not sit under `.claude/worktrees`. An agent that asks for
no isolation, a fork and a remote agent are not affected. The person can approve the launch once.

No NATIVE_DENY: what it denies depends on the folder the session is in, which a pattern on a call can't see. A folder it
cannot read is a deny, and so is an error in the guard (the gate fails closed on a call before it runs).
"""

from __future__ import annotations

import os

from ws.guards import Finding, known_keys

NAME = "guard-launch"
EVENTS = ("pre",)
KINDS = ("agent",)
PRIORITY = 10


def config(table: dict, where: str) -> None:
    known_keys(table, where, ())


def _top(folder: str) -> str | None:
    """The first folder going up that has a `.git` entry, or None."""
    here = os.path.realpath(folder)
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _is_main_checkout(folder: str) -> bool:
    top = _top(folder)
    if top is None or os.path.islink(os.path.join(top, ".git")) or not os.path.isdir(os.path.join(top, ".git")):
        return False
    return "/.claude/worktrees/" not in top + "/"


def judge(call, cfg, ctx) -> Finding | None:
    data = call.data or {}
    if data.get("tool") != "Workflow" and data.get("isolation") != "worktree":
        return None
    if _is_main_checkout(ctx.cwd):
        return None
    what = "a workflow" if data.get("tool") == "Workflow" else "an isolated agent"
    return Finding(
        "deny",
        f"guard-launch: {what} starts only from the main checkout of a git repository, and this session is in "
        f"{ctx.cwd}, which is not one (no repository, or a linked worktree). Start the session in the main checkout, "
        "or run the work without isolation.",
        approvable=True,
    )
