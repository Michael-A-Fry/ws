"""config-guard: the files that hold the guardrails are edited only by the session that owns them.

`paths` lists files and folders to protect (absolute or `~/`); `owners` lists the repositories whose sessions may edit them
(absolute or `~/`, each the root of a repository's main checkout). A session is an owner when the folder it is in lies inside a
main checkout whose root (`git rev-parse --show-toplevel`) is exactly one of the owners: the main checkout and any folder inside
it, but not a linked worktree of it, even one kept under it, not a folder outside any repository, and not a repository whose
`core.worktree` points somewhere else (a session can set that in its own repository's config). A folder under `<root>/.claude/worktrees/` or under a worktree git lists (a prunable one too)
never owns. This is defence in depth, not containment: a session that can write inside the owner's tree isn't fully contained by this check
alone. If git can't say, the session is not an owner. A write tool call (Edit, Write, MultiEdit, NotebookEdit) whose real
path is a protected file, or inside a protected folder, is denied unless the session is an owner, and the reason names the owners.
Links are followed on both sides, so a path through a symlink is the file it points at. A path that can't be resolved
to a real one is a deny. The person can approve the edit once.

The session has to be an owner in both senses: the folder it started in (which the model can't change) and the folder
the call says it runs in. A relative path is joined to that folder, and an empty or relative folder is a deny. On a
platform whose file names ignore case, names are compared without case. A write through a symbolic link, or to a file
with more than one name, is denied to a session that isn't an owner, because a link can be moved onto a guarded file
between the check and the write (a link the session's folder is itself behind, such as /tmp, doesn't count). This guard
judges the edit tools only: a shell redirect to a guarded file is for the sandbox's write-deny list, not this guard.
The same list, and not the link rule, covers a folder swapped for a link after the check, by a command that runs in the
background.

With no `paths` the guard does nothing: ws names no file of its own here (its own policy and settings are held by the
gate and the sandbox). It has no NATIVE_DENY because who owns a file depends on the session's folder, which a runner's
rule can't see. A crash is a deny, as for every guard before a call.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from ws import policy
from ws.guards import Finding, _git, known_keys, text_list
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
    """Is `folder` (an absolute path) inside the main checkout of an owner, by the checkout's root and not by a folder prefix? A linked
    worktree isn't a main checkout, nor is a repository whose `core.worktree` points elsewhere, and git not answering is a no."""
    if not folder or not os.path.isabs(folder):
        return False
    top = _git.main_toplevel(folder)
    if top is None:
        return False
    return _real(top) in {_real(o) for o in cfg.owners}


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
    if _through_a_link(written, [_start_folder()]):  # only the start folder's own path is exempt: `cd` can't add one
        return _deny(call, owners, "a link on the way to it could be moved onto a guarded file in the same turn")
    if _hard_linked(written):
        return _deny(call, owners, "it is a file with more than one name, so it may be a guarded one")
    return None


def _deny(call, owners: str, why: str) -> Finding:
    extra = f" ({why})" if why else ""
    return Finding(
        "deny",
        f"config-guard: {call.value} holds guardrails and is edited only by the session that owns it{extra} "
        f"(owner repositories: {owners}). Start a session in one of them to change it.",
        approvable=True,
    )
