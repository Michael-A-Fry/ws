"""config-guard: the files that hold the guardrails are edited only by the session that owns them.

`paths` lists files and folders to protect (absolute or `~/`); `owners` lists the folders whose sessions may edit them
(absolute or `~/`; a session is in one when its folder is that folder or inside it, a linked worktree kept elsewhere is
a different place). A write tool call (Edit, Write, MultiEdit, NotebookEdit) whose real path is a protected file, or
inside a protected folder, is denied unless the session's folder is an owner's, and the reason names the owner folders.
Links are followed on both sides, so a path through a symlink is the file it points at. A path that can't be resolved
to a real one is a deny. The person can approve the edit once.

With no `paths` the guard does nothing: ws names no file of its own here (its own policy and settings are held by the
gate and the sandbox). It has no NATIVE_DENY because who owns a file depends on the session's folder, which a runner's
rule can't see. A crash is a deny, as for every guard before a call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "config-guard"
EVENTS = ("pre",)
KINDS = ("write",)
PRIORITY = 10


@dataclass(frozen=True)
class Config:
    paths: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()


def _abs_list(table: dict, key: str, where: str) -> tuple[str, ...]:
    out = text_list(table, key, where)
    for p in out:
        if not (p.startswith(("/", "~/")) or p == "~") or "\n" in p or "\0" in p:
            raise PolicyError(f"{where}: {key}: {p!r} must be an absolute path or start with ~/")
    return tuple(out)


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("paths", "owners"))
    return Config(_abs_list(table, "paths", where), _abs_list(table, "owners", where))


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def judge(call, cfg: Config, ctx) -> Finding | None:
    if not cfg.paths:
        return None
    target = _real(call.value)
    if not any(_inside(target, _real(p)) for p in cfg.paths):
        return None
    here = _real(ctx.cwd)
    if any(_inside(here, _real(o)) for o in cfg.owners):
        return None
    owners = ", ".join(cfg.owners) or "none named"
    return Finding(
        "deny",
        f"config-guard: {call.value} holds guardrails and is edited only by the session that owns it (owner folders: "
        f"{owners}). Start a session there to change it.",
        approvable=True,
    )
