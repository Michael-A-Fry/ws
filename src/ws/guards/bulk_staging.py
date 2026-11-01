"""bulk-staging: a commit holds only the files the session names. `git add -A`, `--all`, `.`, `./` and `:/`, and
`git commit -a`, `-am` and `--all` (with or without git's global options such as `-C dir`) are stopped; `git add file`,
`git add -- file` and `git commit -m msg` go through. Text inside quotes and here-document bodies is not a command, so
`git commit -m "git add -A"` and a message passed as a here-document are fine.

The runner's own deny rules carry the plain spellings too (NATIVE_DENY), so they hold with no hook.
"""

from __future__ import annotations

from ws import policy
from ws.guards import Finding, known_keys

NAME = "bulk-staging"
EVENTS = ("pre",)
KINDS = ("command",)
NATIVE_DENY = (
    "git add -A*",
    "git add --all*",
    "git add .",
    "git add ./",
    "git add :/",
    "git commit -a*",
    "git commit --all*",
    "git -* add -A*",
    "git -* add --all*",
    "git -* add .",
    "git -* add ./",
    "git -* add :/",
    "git -* commit -a*",
    "git -* commit --all*",
)

_COMMIT_TAKES_VALUE = set("mFCcu")  # short commit options that read a value: the rest of the cluster or the next word
_COMMIT_LONG_VALUE = {"--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message", "--fixup"}


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def _stages_everything(words: list[str]) -> str | None:
    sub = words[1] if len(words) > 1 else ""
    rest = words[2:]
    if sub == "add":
        for w in rest:
            if w == "--":
                break
            if w == "--all" or (w.startswith("-") and not w.startswith("--") and "A" in w):
                return "git add -A stages everything"
        operands = _operands(rest)
        if any(o.rstrip("/") in (".", ":") for o in operands):
            return "git add of the whole tree stages everything"
    if sub == "commit":
        skip = False
        for w in rest:
            if skip:
                skip = False
            elif w == "--":
                break
            elif w == "--all":
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
    for words in policy.simple_commands(call.value):
        if words[0] != "git":
            continue
        why = _stages_everything(words)
        if why:
            return Finding("deny", f"{why}; name the files: `git add path ...`, then `git commit -m ...`")
    return None
