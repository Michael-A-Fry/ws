"""checkout-pin: a checkout that has linked worktrees (the shared main checkout), and any checkout `paths` names, stays
where it is. A command that moves its HEAD or rewrites its working tree is denied, and the reason says to make a
worktree and work there. The person can approve the command once.

Covered, for a `git` command that runs in such a checkout (its own folder, a folder inside it, `git -C DIR`,
`--work-tree DIR`, `GIT_WORK_TREE=DIR`, or after a `cd`): `checkout` and `switch` to a branch or commit (also `-b`,
`-B`, `-c`, `-C`, `--detach`, `--orphan`, `-f`), `reset --hard`, `--merge` and `--keep`, `rebase`, `merge`, `pull`,
`cherry-pick`, `revert`, and `worktree move` to a destination inside it. `--abort` and `--quit` of a rebase, merge,
cherry-pick or revert go through, so a half-done one can be undone. Not covered: `checkout -- FILE` (and `checkout
TREE -- FILE`), `restore`, `commit`, `fetch`, `status`, `log`, `diff`, `add`, `stash` and a soft or mixed `reset`.
A checkout with no linked worktrees that `paths` doesn't name is free, and so is a linked worktree, so a branch switch
inside a worktree goes through.

A checkout is found from the folder up: the first folder with a `.git` entry is its top. It has linked worktrees when
`.git` is a folder holding an entry under `worktrees/`; a linked worktree's `.git` is a file, so that rule never pins
it. `paths` lists checkouts to pin whatever their worktrees (absolute or `~/`); each is matched, links followed, by the
top of the checkout, so a worktree kept inside a pinned checkout isn't pinned with it.

The folder a command runs in is read from literal words, as the shell would. A folder that comes from a variable or
substitution (`git -C "$DIR" ...`, `cd "$(...)"`) can't be read, so a command that would move HEAD from there is
denied and the reason asks for a path. The guard is advice-level: a script run by `bash script.sh` isn't read.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

from ws import policy
from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "checkout-pin"
EVENTS = ("pre",)
KINDS = ("command",)

_CHECKOUT_VALUE = {"--conflict", "--pathspec-from-file", "-U", "--unified", "--inter-hunk-context"}
_UNDO_ONLY = {  # subcommand -> the options that go through (they end or inspect a half-done operation)
    "rebase": {"--abort", "--quit", "--show-current-patch"},
    "merge": {"--abort", "--quit"},
    "cherry-pick": {"--abort", "--quit"},
    "revert": {"--abort", "--quit"},
}


@dataclass(frozen=True)
class Config:
    paths: tuple[str, ...] = ()


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("paths",))
    paths = text_list(table, "paths", where)
    for p in paths:
        if not (p.startswith(("/", "~/")) or p == "~") or "\n" in p or "\0" in p:
            raise PolicyError(f"{where}: paths: {p!r} must be an absolute path or start with ~/")
    return Config(paths)


# --- which command moves HEAD -------------------------------------------------------------------------------------


def _long(word: str, name: str) -> bool:
    """Is `word` the long option `name`, or a prefix of it git reads as it (`--hard`, `--har`, `--hard=x`)?"""
    return word.startswith("--") and len(word) >= 4 and name.startswith(word.split("=", 1)[0])


def _split(args: list[str]) -> tuple[list[str], bool]:
    """(the words before a `--`, whether there was one)."""
    if "--" in args:
        return args[: args.index("--")], True
    return args, False


def _operands(words: list[str], takes_value: set[str] = frozenset()) -> list[str]:
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
        elif w.startswith("-") and len(w) > 1:
            skip = w in takes_value
        else:
            out.append(w)
    return out


def _moves_checkout(before: list[str], dashes: bool) -> bool:
    for w in before:
        if w.startswith("--"):
            if any(_long(w, n) for n in ("--detach", "--orphan", "--force")):
                return True
        elif w.startswith("-") and len(w) > 1 and set(w[1:]) & set("bBfd"):
            return True
    return not dashes and bool(_operands(before, _CHECKOUT_VALUE))


def _moves_reset(before: list[str]) -> bool:
    return any(_long(w, n) for w in before for n in ("--hard", "--merge", "--keep"))


def _moves(sub: str, args: list[str]) -> str | None:
    """What the command does to HEAD or the working tree, as text, or None when it does neither."""
    if "-h" in args or "--help" in args:
        return None
    before, dashes = _split(args)
    if sub == "checkout" and _moves_checkout(before, dashes):
        return "git checkout to another branch or commit"
    if sub in ("switch", "pull"):
        return f"git {sub}"
    if sub == "reset" and _moves_reset(before):
        return "git reset --hard (or --merge, --keep)"
    if sub in _UNDO_ONLY and not any(a in _UNDO_ONLY[sub] for a in args):
        return f"git {sub}"
    return None


def _worktree_move_dest(args: list[str]) -> str | None:
    """The new path of `git worktree move [options] WORKTREE NEW-PATH`, or None."""
    if not args or args[0] != "move":
        return None
    operands = _operands(args[1:], {"--reason"})
    return operands[1] if len(operands) >= 2 else None


# --- where it runs ------------------------------------------------------------------------------------------------


def _unreadable(path: str) -> bool:
    return "$" in path or "`" in path


def _join(here: str | None, path: str) -> str | None:
    """`path` as the shell would reach it from `here`; None when that can't be told."""
    if _unreadable(path):
        return None
    path = os.path.expanduser(path)
    if posixpath.isabs(path) or os.path.isabs(path):
        return path
    return None if here is None else os.path.join(here, path)


def _cd(here: str | None, words: list[str]) -> str | None:
    args = [w for w in words[1:] if not w.startswith("-") or w == "-"]
    if not args:
        return os.path.expanduser("~")
    return None if args[0] == "-" else _join(here, args[0])


def _folder(here: str | None, options: list[str], env: list[str]) -> str | None:
    """The folder a git command with these global options and `VAR=value` words runs in, or None when unreadable."""
    folder = here
    for a in env:
        if a.startswith("GIT_WORK_TREE="):
            folder = _join(folder, a.partition("=")[2])
    i = 0
    while i < len(options):
        w, value = options[i], None
        if w in ("-C", "--work-tree"):
            value = options[i + 1] if i + 1 < len(options) else ""
            i += 1
        elif w.startswith("--work-tree="):
            value = w.partition("=")[2]
        if value is not None:
            folder = _join(folder, value)
        i += 1
    return folder


def _top(path: str) -> str | None:
    """The top of the checkout `path` is in: the first folder from it upward that has a `.git` entry."""
    p = os.path.realpath(path)
    while True:
        if os.path.lexists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _has_worktrees(top: str) -> bool:
    try:
        with os.scandir(os.path.join(top, ".git", "worktrees")) as entries:
            return any(e.is_dir() for e in entries)
    except OSError:  # no such folder, or `.git` is a file: a linked worktree
        return False


def _pinned(path: str, cfg: Config) -> tuple[str, str] | None:
    """(the top of the checkout, why it is pinned) when `path` is in a pinned checkout; else None."""
    top = _top(path)
    if top is None:
        return None
    if any(top == os.path.realpath(os.path.expanduser(p)) for p in cfg.paths):
        return top, "the policy names in `paths`"
    if _has_worktrees(top):
        return top, "has linked worktrees"
    return None


# --- the guard ----------------------------------------------------------------------------------------------------


def judge(call, cfg: Config, ctx) -> Finding | None:
    here: str | None = ctx.cwd or os.getcwd()
    for words, env in policy.simple_commands_env(call.value, git_options=True):
        if not words:
            continue
        if words[0] in ("cd", "pushd"):
            here = _cd(here, words)
            continue
        if words[0] != "git":
            continue
        options, words = policy.git_split(words)
        sub, args = (words[1] if len(words) > 1 else ""), words[2:]
        folder = _folder(here, options, env)
        if sub == "worktree" and (dest := _worktree_move_dest(args)) is not None:
            target = None if folder is None else _join(folder, dest)
            what, effect = "git worktree move", "would put a worktree inside"
            advice = "Move the worktree to a folder outside it."
        elif what := _moves(sub, args):
            target, effect = folder, "would move HEAD or rewrite the working tree of"
            advice = "Work in a worktree instead: `git worktree add ../NAME -b BRANCH`, then run the command there."
        else:
            continue
        if target is None:
            return Finding(
                "deny",
                f"{what} would move HEAD or rewrite a working tree, and the folder it runs in comes from a "
                "variable or substitution, so the guard can't tell whether it is a pinned checkout. Give the folder "
                "as a path (`git -C /path ...`), or work in a worktree.",
                approvable=True,
            )
        found = _pinned(target, cfg)
        if found:
            top, why = found
            return Finding(
                "deny",
                f"{what} {effect} {top}, a checkout that {why}. {advice}",
                approvable=True,
            )
    return None
