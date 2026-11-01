"""bulk-staging: a commit holds only the files the session names. `git add -A`, `--all` (and any prefix of it, such as
`--al`), `.`, `./`, `*`, `:/` and `:(top)`, the same with `git stage`, and `git commit -a`, `-am` and `--all` (with or
without git's global options such as `-C dir`) are stopped, and so is `git -c alias.NAME=...`, which can define a
command that stages everything under a name this check can't see. `git add file`, `git add -- file` and
`git commit -m msg` go through. Text inside quotes, comments and here-document bodies is not a command, so
`git commit -m "git add -A"` and a message passed as a here-document are fine; the text after `eval` and in
`bash -c '...'` is a command and is read.

The runner's own deny rules carry the plain spellings too (NATIVE_DENY), so they hold with no hook.
"""

from __future__ import annotations

import re

from ws import policy
from ws.guards import Finding, known_keys

NAME = "bulk-staging"
EVENTS = ("pre",)
KINDS = ("command",)
NATIVE_DENY = (
    "git add -A*",
    "git add --a*",
    "git add .",
    "git add ./",
    "git add :/",
    "git stage -A*",
    "git stage --a*",
    "git stage .",
    "git stage ./",
    "git stage :/",
    "git commit -a*",
    "git commit --all*",
    "git -* add -A*",
    "git -* add --a*",
    "git -* add .",
    "git -* add ./",
    "git -* add :/",
    "git -* stage -A*",
    "git -* stage --a*",
    "git -* stage .",
    "git -* stage ./",
    "git -* stage :/",
    "git -* commit -a*",
    "git -* commit --all*",
    "git -c alias.*",
    "git * -c alias.*",
)

_COMMIT_TAKES_VALUE = set("mFCcu")  # short commit options that read a value: the rest of the cluster or the next word
_COMMIT_LONG_VALUE = {"--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message", "--fixup"}
_STAGES = ("add", "stage")  # `git stage` is git's own name for `git add`
_MAGIC = re.compile(r":\(([^)]*)\)(.*)", re.DOTALL)


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def _all_option(w: str) -> bool:
    """`--all`, or a prefix git reads as it (`--a`, `--al`)."""
    return w.startswith("--") and len(w) > 2 and "--all".startswith(w)


def _whole_tree(pathspec: str) -> bool:
    """Does this pathspec name the top of the tree: `.`, `./`, `*`, `:/`, `:(top)` and `:(top).`?"""
    magic = _MAGIC.fullmatch(pathspec)
    if magic:
        return "top" in magic.group(1).split(",") and magic.group(2).rstrip("/") in ("", ".", "*")
    if pathspec.startswith(":/"):
        return pathspec[2:].rstrip("/") in ("", ".", "*")
    return pathspec.rstrip("/") in (".", ":", "*")


def _alias_option(options: list[str]) -> bool:
    """Do git's global options define an alias (`-c alias.x=...`, `-calias.x=...`, `--config-env=alias.x=VAR`)?"""
    for i, w in enumerate(options):
        value = options[i + 1] if i + 1 < len(options) else ""
        if w in ("-c", "--config-env") and value.startswith("alias."):
            return True
        if w.startswith(("-calias.", "--config-env=alias.")):
            return True
    return False


def _stages_everything(words: list[str]) -> str | None:
    sub = words[1] if len(words) > 1 else ""
    rest = words[2:]
    if sub in _STAGES:
        for w in rest:
            if w == "--":
                break
            if _all_option(w) or (w.startswith("-") and not w.startswith("--") and "A" in w):
                return f"git {sub} -A stages everything"
        if any(_whole_tree(o) for o in _operands(rest)):
            return f"git {sub} of the whole tree stages everything"
    if sub == "commit":
        skip = False
        for w in rest:
            if skip:
                skip = False
            elif w == "--":
                break
            elif _all_option(w):
                return "git commit --all stages everything"
            elif w.startswith("--"):
                skip = w in _COMMIT_LONG_VALUE
            elif w.startswith("-") and len(w) > 1:
                for i, letter in enumerate(w[1:], 1):
                    if letter == "a":
                        return "git commit -a stages everything"
                    if letter in _COMMIT_TAKES_VALUE:
                        skip = i == len(w) - 1  # the value is the next word unless it is glued on
                        break
    return None


def _operands(rest: list[str]) -> list[str]:
    """The pathspecs of `git add`: every word after `--`, and each word that is not an option."""
    out: list[str] = []
    after = False
    for w in rest:
        if after:
            out.append(w)
        elif w == "--":
            after = True
        elif not w.startswith("-"):
            out.append(w)
    return out


def judge(call, cfg, ctx) -> Finding | None:
    for words in policy.simple_commands(call.value, git_options=True):
        if words[0] != "git":
            continue
        options, words = policy.git_split(words)
        why = "git -c alias.NAME=... can define a command that stages everything" if _alias_option(options) else None
        why = why or _stages_everything(words)
        if why:
            return Finding("deny", f"{why}; name the files: `git add path ...`, then `git commit -m ...`")
    return None
