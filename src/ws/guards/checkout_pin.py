"""checkout-pin: a checkout that has linked worktrees (the shared main checkout), and any checkout `paths` names, stays
where it is. A command that moves its HEAD or rewrites its working tree is denied, and the reason says to make a
worktree and work there. The person can approve the command once.

Covered, for a `git` command that runs in such a checkout (its own folder, a folder inside it, `git -C DIR`,
`--work-tree DIR`, `GIT_WORK_TREE=DIR`, `--git-dir DIR` and `GIT_DIR=DIR` of it, or after a `cd`): `checkout` and
`switch` to a branch or commit (also `-b`, `-B`, `-c`, `-C`, `--detach`, `--orphan`, `-f`), `checkout REF -- .` and
`restore --source=REF .` (the whole tree from another ref), `reset --hard`, `--merge` and `--keep`, `rebase`, `merge`,
`pull`, `cherry-pick`, `revert`, `am`, `bisect` (but `log`, `view`, `visualize`, `help` and `terms`), `stash pop`,
`apply` and `branch`, and `worktree move` to a destination inside it. `--abort` and `--quit` of a rebase, merge,
cherry-pick, revert or am go through, so a half-done one can be undone. A `-c alias.NAME=...` that names one of these is
read as that command. Not covered: `checkout -- FILE` (and `checkout TREE -- FILE`), `restore` of files, `commit`,
`fetch`, `status`, `log`, `diff`, `add`, `stash` and a soft or mixed `reset`, and an alias from a config file or the
environment. A checkout with no linked worktrees that `paths` doesn't name is free, and so is a linked worktree, so a
branch switch inside a worktree goes through.

A checkout is found from the folder up: the first folder with a `.git` entry is its top. It has linked worktrees when
`.git` is a folder holding an entry under `worktrees/`; a linked worktree's `.git` is a file, so that rule never pins
it. `paths` lists checkouts to pin whatever their worktrees (absolute or `~/`); each is matched, links followed, by the
top of the checkout, so a worktree kept inside a pinned checkout isn't pinned with it. A git dir belongs to the
checkout that holds it (`MAIN/.git`), or, for a linked worktree's, to that worktree.

The folder a command runs in is read from literal words, as the shell would. Where a `cd` leaves the shell is kept as
a set of folders: a subshell, a pipe, a background command, a `cd` after `&&` or `||` and a `cd` to a folder that
doesn't exist can each leave it where it was, so a move is judged in every folder it could be in. A folder that comes
from a variable or substitution (`git -C "$DIR" ...`, `cd "$(...)"`), and a `GIT_DIR` or `GIT_WORK_TREE` set earlier by
`export` or `declare`, can't be read, so a command that would move HEAD from there is denied and the reason asks for a
path. The guard is advice-level: a script run by `bash script.sh` isn't read.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

from ws import policy, shellwords
from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "checkout-pin"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10

_CHECKOUT_VALUE = {"--conflict", "--pathspec-from-file", "-U", "--unified", "--inter-hunk-context"}
_RESTORE_VALUE = {"--source", "-s", "--pathspec-from-file", "-U", "--unified", "--inter-hunk-context"}
_UNDO_ONLY = {  # subcommand -> the options that go through (they end or inspect a half-done operation)
    "rebase": {"--abort", "--quit", "--show-current-patch"},
    "merge": {"--abort", "--quit"},
    "cherry-pick": {"--abort", "--quit"},
    "revert": {"--abort", "--quit"},
    "am": {"--abort", "--quit", "--show-current-patch"},
}
_BISECT_READS = {"log", "view", "visualize", "help", "terms"}
_STASH_MOVES = {"pop", "apply", "branch"}
_SETTERS = {"export", "declare", "typeset", "readonly", "local"}  # words that can put a variable in the environment
_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE")


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


def _whole_tree(pathspec: str) -> bool:
    """Is this pathspec the top of the tree (`.`, `./`, `:/`, `:(top)`, `*`)?"""
    if pathspec == "*":
        return True
    for magic in (":(top)", ":/"):
        if pathspec.startswith(magic):
            rest = pathspec[len(magic) :]
            return rest in ("", "*") or posixpath.normpath(rest) == "."
    return posixpath.normpath(pathspec) == "."


def _moves_checkout(before: list[str], after: list[str], dashes: bool) -> str | None:
    for w in before:
        if w.startswith("--"):
            if any(_long(w, n) for n in ("--detach", "--orphan", "--force")):
                return "git checkout to another branch or commit"
        elif w.startswith("-") and len(w) > 1 and set(w[1:]) & set("bBfd"):
            return "git checkout to another branch or commit"
    operands = _operands(before, _CHECKOUT_VALUE)
    if not dashes and operands:
        return "git checkout to another branch or commit"
    if dashes and operands and any(_whole_tree(p) for p in after):
        return "git checkout REF -- . (the whole tree from another ref)"
    return None


def _moves_restore(args: list[str]) -> bool:
    """`git restore` with a source and the whole tree as its pathspec."""
    sourced, pathspecs, skip = False, [], False
    for i, w in enumerate(args):
        if skip:
            skip = False
        elif w == "--":
            pathspecs += args[i + 1 :]
            break
        elif w.startswith("--"):
            sourced |= _long(w, "--source")
            skip = w in _RESTORE_VALUE
        elif w.startswith("-") and len(w) > 1:
            at = w.find("s", 1)  # `-s REF`, `-sREF`, or the s of a cluster such as `-Ws REF`
            sourced |= at > 0
            skip = at == len(w) - 1
        else:
            pathspecs.append(w)
    return sourced and any(_whole_tree(p) for p in pathspecs)


def _moves_reset(before: list[str]) -> bool:
    return any(_long(w, n) for w in before for n in ("--hard", "--merge", "--keep"))


def _moves(sub: str, args: list[str]) -> str | None:
    """What the command does to HEAD or the working tree, as text, or None when it does neither."""
    if "-h" in args or "--help" in args:
        return None
    before, dashes = _split(args)
    if sub == "checkout" and (what := _moves_checkout(before, args[len(before) + 1 :], dashes)):
        return what
    if sub == "restore" and _moves_restore(args):
        return "git restore --source=REF . (the whole tree from another ref)"
    if sub in ("switch", "pull"):
        return f"git {sub}"
    if sub == "reset" and _moves_reset(before):
        return "git reset --hard (or --merge, --keep)"
    if sub == "stash" and args and args[0] in _STASH_MOVES:
        return f"git stash {args[0]}"
    if sub == "bisect" and args and args[0] not in _BISECT_READS:
        return f"git bisect {args[0]}"
    if sub in _UNDO_ONLY and not any(a.split("=", 1)[0] in _UNDO_ONLY[sub] for a in args):
        return f"git {sub}"
    return None


def _aliases(options: list[str]) -> dict[str, str]:
    """The `-c alias.NAME=VALUE` options of a git command, by lower-case name."""
    found: dict[str, str] = {}
    for i, w in enumerate(options[:-1]):
        key, _, value = options[i + 1].partition("=")
        if w == "-c" and key.lower().startswith("alias."):
            found[key[6:].lower()] = value
    return found


def _alias_moves(sub: str, args: list[str], aliases: dict[str, str], depth: int = 0) -> str | None:
    """_moves, reading `sub` through the aliases when it is not itself a git command (git ignores an alias that
    shadows one)."""
    if (what := _moves(sub, args)) or sub.lower() not in aliases or depth > 4:
        return what
    value = aliases[sub.lower()]
    if value.startswith("!"):
        for words, _ in policy.simple_commands_env(value[1:], git_options=True):
            if words[:1] == ["git"]:
                _, rest = policy.git_split(words)
                if len(rest) > 1 and (what := _alias_moves(rest[1], rest[2:] + args, aliases, depth + 1)):
                    return what
        return None
    try:
        shellwords.require_closed(value)
        parts = shellwords.words_of(value)
    except ValueError:  # shellwords.ShellError is one
        return f"git {sub} (an alias that can't be read)"
    if not parts:
        return None
    _, rest = policy.git_split(["git", *parts])
    if len(rest) < 2:
        return None
    return _alias_moves(rest[1], rest[2:] + args, aliases, depth + 1)


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


def _places(folder: str | None, options: list[str], env: list[str]) -> list[tuple[str, str | None]]:
    """What a git command with these global options and `VAR=value` words acts on, run from `folder`: the working
    tree as ("dir", path) and, when a git dir is given, ("git", path). A path is None when it can't be read."""
    cwd = folder
    given: dict[str, str | None] = {}
    i = 0
    while i < len(options):
        w, value = options[i], None
        if w in ("-C", "--work-tree", "--git-dir", "-c"):
            value = options[i + 1] if i + 1 < len(options) else ""
            i += 1
        elif w.startswith(("--work-tree=", "--git-dir=")):
            value = w.partition("=")[2]
            w = w.partition("=")[0]
        if value is not None:
            if w == "-C":
                cwd = _join(cwd, value)
            elif w == "--work-tree":
                given["tree"] = _join(cwd, value)
            elif w == "--git-dir":
                given["git"] = _join(cwd, value)
            elif w == "-c" and value.lower().startswith("core.worktree="):
                given.setdefault("config-tree", _join(cwd, value.partition("=")[2]))
        i += 1
    for a in env:
        name, _, value = a.partition("=")
        if name == "GIT_WORK_TREE":
            given.setdefault("tree", _join(cwd, value))
        elif name == "GIT_DIR":
            given.setdefault("git", _join(cwd, value))
    tree = given["tree"] if "tree" in given else given.get("config-tree", cwd)
    places: list[tuple[str, str | None]] = [("dir", tree)]
    if "git" in given:
        places.append(("git", given["git"]))
    return places


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


def _has_worktrees(git_dir: str) -> bool:
    try:
        with os.scandir(os.path.join(git_dir, "worktrees")) as entries:
            return any(e.is_dir() for e in entries)
    except OSError:  # no such folder, or the git dir is a file: a linked worktree
        return False


def _named(path: str, cfg: Config) -> bool:
    return any(path == os.path.realpath(os.path.expanduser(p)) for p in cfg.paths)


def _pinned(path: str, cfg: Config) -> tuple[str, str] | None:
    """(the top of the checkout, why it is pinned) when `path` is in a pinned checkout; else None."""
    top = _top(path)
    if top is None:
        return None
    if _named(top, cfg):
        return top, "the policy names in `paths`"
    if _has_worktrees(os.path.join(top, ".git")):
        return top, "has linked worktrees"
    return None


def _read_line(path: str, prefix: str = "") -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            line = f.readline().strip()
    except OSError:
        return None
    return line[len(prefix) :].strip() if line.startswith(prefix) else None


def _pinned_git_dir(path: str, cfg: Config) -> tuple[str, str] | None:
    """_pinned for a git dir: the checkout it belongs to. `MAIN/.git` belongs to MAIN; a linked worktree's git dir
    (`MAIN/.git/worktrees/NAME`) to that worktree; a `.git` file leads to the git dir it names. A bare repository is
    pinned when `paths` names it or it has linked worktrees."""
    p = os.path.realpath(path)
    if os.path.isfile(p):  # a `.git` file: `gitdir: PATH`
        target = _read_line(p, "gitdir:")
        if not target:
            return p, "has a git dir ws can't read"
        p = os.path.realpath(os.path.join(os.path.dirname(p), target))
    link = os.path.join(p, "gitdir")
    if os.path.isfile(link) and os.path.isfile(os.path.join(p, "commondir")):
        dotgit = _read_line(link)
        if not dotgit:
            return p, "has a git dir ws can't read"
        return _pinned(os.path.dirname(os.path.realpath(dotgit)), cfg)
    if os.path.basename(p) == ".git":
        return _pinned(os.path.dirname(p), cfg)
    if _named(p, cfg):
        return p, "the policy names in `paths`"
    return (p, "has linked worktrees") if _has_worktrees(p) else None


# --- the guard ----------------------------------------------------------------------------------------------------


def _unknown(what: str) -> Finding:
    return Finding(
        "deny",
        f"{what} would move HEAD or rewrite a working tree, and the folder it runs in comes from a "
        "variable or substitution, so the guard can't tell whether it is a pinned checkout. Give the folder "
        "as a path (`git -C /path ...`), or work in a worktree.",
        approvable=True,
    )


def _union(*sets: list[str | None]) -> list[str | None]:
    return list(dict.fromkeys(x for s in sets for x in s))


def _cd_into(base: list[str | None], words: list[str]) -> list[str | None]:
    """Where the shell is after `cd` from any of the folders in `base`. A folder that doesn't exist leaves it where it
    was, and one that can't be read leaves it unknown (None)."""
    out: list[str | None] = []
    for folder in base:
        target = _cd(folder, words)
        if target is None:
            out.append(None)
        elif os.path.isdir(target):
            out.append(target)
        else:
            out.append(folder)
    return _union(out)


def _sets_location(words: list[str]) -> bool:
    """Does this `export`/`declare` word put GIT_DIR or GIT_WORK_TREE in the environment?"""
    return words[0] in _SETTERS and any(w.partition("=")[0] in _LOCATION_VARS for w in words[1:])


def _judge_git(words: list[str], env: list[str], view: list[str | None], exported: bool, cfg: Config):
    options, words = policy.git_split(words)
    sub, args = (words[1] if len(words) > 1 else ""), words[2:]
    dest = _worktree_move_dest(args) if sub == "worktree" else None
    if dest is not None:
        what, effect = "git worktree move", "would put a worktree inside"
        advice = "Move the worktree to a folder outside it."
    elif what := _alias_moves(sub, args, _aliases(options)):
        effect = "would move HEAD or rewrite the working tree of"
        advice = "Work in a worktree instead: `git worktree add ../NAME -b BRANCH`, then run the command there."
    else:
        return None
    if exported and dest is None:
        return _unknown(what)
    for folder in view:
        places = _places(folder, options, env)
        if dest is not None:
            places = places[:1]
        for kind, path in places:
            if dest is not None and path is not None:
                path = _join(path, dest)
            if path is None:
                return _unknown(what)
            found = _pinned_git_dir(path, cfg) if kind == "git" else _pinned(path, cfg)
            if found:
                top, why = found
                return Finding("deny", f"{what} {effect} {top}, a checkout that {why}. {advice}", approvable=True)
    return None


def judge(call, cfg: Config, ctx) -> Finding | None:
    # Where the shell could be: `here` after the commands so far, `chain` when every command so far in this
    # `&&` chain passed (the folder a command after `&&` runs in), and the states to return to at a `)`.
    here: list[str | None] = [ctx.cwd or os.getcwd()]
    chain = list(here)
    exported = False
    saved: list[tuple[list[str | None], list[str | None], bool]] = []
    before = (here, chain)
    seen = list(here)
    for words, env, link, piped in policy.simple_commands_flow(call.value, git_options=True):
        if not words and not env:
            if link in ("(", "(?"):
                saved.append((here, chain, exported))
                here = _union(seen) if link == "(?" else here
                chain = list(here)
            elif link == ")" and saved:
                here, chain, exported = saved.pop()
            elif link == "&":  # the command before it ran in the background: it changed nothing here
                here, chain = before
            continue
        if link == ";":
            chain = list(here)
        before = (here, chain)
        view = chain if link == "&&" else here
        if words and words[0] in ("cd", "pushd") and not piped:
            new = _cd_into(view, words)
            if link == ";":
                here = chain = new
            elif link == "&&":
                here, chain = _union(here, new), new
            else:
                here, chain = _union(here, new), _union(chain, new)
            seen = _union(seen, new)
        elif words and _sets_location(words):
            exported = True
        elif words and words[0] == "git":
            if found := _judge_git(words, env, view, exported, cfg):
                return found
    return None
