"""config-guard: the files that hold the guardrails are edited only by the session that owns them.

`paths` lists files and folders to protect (absolute or `~/`); `owners` lists the folders whose sessions may edit them
(absolute or `~/`; a session is in one when its folder is that folder or inside it, a linked worktree kept elsewhere is
a different place). A write tool call (Edit, Write, MultiEdit, NotebookEdit) whose real path is a protected file, or
inside a protected folder, is denied unless the session's folder is an owner's, and the reason names the owner folders.
Links are followed on both sides, so a path through a symlink is the file it points at. A path that can't be resolved
to a real one is a deny. The person can approve the edit once.

The session has to be an owner in both senses: the folder it started in (which the model can't change) and the folder
the call says it runs in. A relative path is joined to that folder, and an empty or relative folder is a deny. On a
platform whose file names ignore case, names are compared without case. A write through a symbolic link, or to a file
with more than one name, is denied to a session that isn't an owner, because a link can be moved onto a guarded file
between the check and the write (a link the session's folder is itself behind, such as /tmp, doesn't count). This guard
judges the edit tools only: a shell redirect to a guarded file is for the sandbox's write-deny list, not this guard.

With no `paths` the guard does nothing: ws names no file of its own here (its own policy and settings are held by the
gate and the sandbox). It has no NATIVE_DENY because who owns a file depends on the session's folder, which a runner's
rule can't see. A crash is a deny, as for every guard before a call.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from ws import policy
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


def _fold(p: str) -> str:
    return p.casefold() if policy.CASELESS_PATHS else p


def _real(p: str) -> str:
    return _fold(os.path.realpath(os.path.expanduser(p)))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _start_folder() -> str:
    from ws.gate import _start_folder as start

    return start()


def _owns(folder: str, cfg: Config) -> bool:
    """Is `folder` (an absolute path) inside one of the owner folders?"""
    if not folder or not os.path.isabs(folder):
        return False
    here = _real(folder)
    return any(_inside(here, _real(o)) for o in cfg.owners)


def _through_a_link(path: str, folders: list[str]) -> bool:
    """Does a folder or file on the way to `path` (as written, before links are followed) sit behind a symbolic link?
    A link the session's own folder is already behind (a system link such as /tmp) doesn't count."""
    walked = "/"
    for part in path.split("/"):
        if not part:
            continue
        walked = os.path.join(walked, part)
        if os.path.islink(walked) and not any(f == walked or f.startswith(walked + "/") for f in folders if f):
            return True
    return False


def _hard_linked(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


def judge(call, cfg: Config, ctx) -> Finding | None:
    if not cfg.paths:
        return None
    where = ctx.cwd
    written = os.path.expanduser(call.value)
    if not os.path.isabs(written):
        written = os.path.join(where, written) if where and os.path.isabs(where) else ""
    folders = [where, _start_folder()]
    owner = all(_owns(f, cfg) for f in folders)
    owners = ", ".join(cfg.owners) or "none named"
    if not written:
        return _deny(call, owners, "it names no folder to resolve it in")
    target = _real(written)
    protected = [_real(p) for p in cfg.paths]
    is_protected = any(_inside(target, p) for p in protected) or any(
        _inside(_fold(os.path.normpath(written)), p) for p in protected
    )
    if owner:
        return None
    if is_protected:
        return _deny(call, owners, "")
    if _through_a_link(written, folders):
        return _deny(call, owners, "a link on the way to it could be moved onto a guarded file in the same turn")
    if _hard_linked(written):
        return _deny(call, owners, "it is a file with more than one name, so it may be a guarded one")
    return None


def _deny(call, owners: str, why: str) -> Finding:
    extra = f" ({why})" if why else ""
    return Finding(
        "deny",
        f"config-guard: {call.value} holds guardrails and is edited only by the session that owns it{extra} "
        f"(owner folders: {owners}). Start a session there to change it.",
        approvable=True,
    )
