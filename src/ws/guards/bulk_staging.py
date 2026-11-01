"""bulk-staging: a commit holds only the files the session names. `git add -A`, `--all` (and any prefix of it, such as
`--al`), `-u` and `--update` with no pathspec, `.`, `./`, `./.`, `*`, `:/` and `:(top)`, the same with `git stage`, and
`git commit -a`, `-am`, `--all` and a commit of the whole tree (`git commit .`, `git commit -i .`) are stopped (with or
without git's global options such as `-C dir`). So is a pathspec that is only a variable or substitution (`"$PWD"`,
`$(pwd)`), which this check can't read. So is an alias: `git -c alias.NAME=...` in any case (git config keys are
not case-sensitive), or `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` naming one, or `GIT_CONFIG_PARAMETERS`, can define a
command that stages everything under a name this check can't see. `git add file`, `git add -- file`, `git add -u src/`
and `git commit -m msg` go through. Text inside quotes, comments and here-document bodies is not a command, so
`git commit -m "git add -A"` and a message passed as a here-document are fine; the text after `eval` and in
`bash -c '...'` is a command and is read.

The runner's own deny rules carry the plain spellings too (NATIVE_DENY), so they hold with no hook.
"""

from __future__ import annotations

import posixpath
import re

from ws import policy
from ws.guards import Finding, known_keys

NAME = "bulk-staging"
EVENTS = ("pre",)
KINDS = ("command",)
_NATIVE_SUBS = (
    "add -A*",
    "add --a*",
    "add .",
    "add ./",
    "add :/",
    "add -u",
    "add --u*",
    "stage -A*",
    "stage --a*",
    "stage .",
    "stage ./",
    "stage :/",
    "stage -u",
    "stage --u*",
    "commit -a*",
    "commit --all*",
    "commit .",
    "commit -- .",
)
NATIVE_DENY = (
    *(f"git {sub}" for sub in _NATIVE_SUBS),
    *(f"git -* {sub}" for sub in _NATIVE_SUBS),
    "git -c alias.*",
    "git -c Alias.*",
    "git -c ALIAS.*",
    "git * -c alias.*",
    "git * -c Alias.*",
    "git * -c ALIAS.*",
)

_COMMIT_TAKES_VALUE = set("mFCct")  # short commit options that read a value: the rest of the cluster or the next word
_COMMIT_LONG_VALUE = {
    "--message",
    "--file",
    "--author",
    "--date",
    "--reuse-message",
    "--reedit-message",
    "--fixup",
    "--squash",
    "--template",
    "--cleanup",
    "--trailer",
    "--pathspec-from-file",
}
_STAGES = ("add", "stage")  # `git stage` is git's own name for `git add`
_MAGIC = re.compile(r":\(([^)]*)\)(.*)", re.DOTALL)
_EXPANSION = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]*\}|\$\(.*\)|`[^`]*`", re.DOTALL)  # the whole word
_GIT_ENV = re.compile(r"GIT_CONFIG_(?:PARAMETERS|KEY_[0-9]+)=", re.IGNORECASE)
_SETS_VARIABLES = {"export", "declare", "typeset", "readonly", "local"}


def config(table: dict, where: str) -> None:
    known_keys(table, where)


def _all_option(w: str) -> bool:
    """`--all`, or a prefix git reads as it (`--a`, `--al`)."""
    return w.startswith("--") and len(w) > 2 and "--all".startswith(w)


def _update_option(w: str) -> bool:
    """`--update`, or a prefix of it, or `-u` in a cluster of short options."""
    if w.startswith("--"):
        return len(w) > 2 and "--update".startswith(w)
    return w.startswith("-") and "u" in w[1:]


def _tree_name(p: str) -> bool:
    """Does this path, once `.` and `//` are folded away, name the top of the tree: `.`, `./.`, `*`, `**`?"""
    return p == "" or posixpath.normpath(p) in (".", "*", "**")


def _whole_tree(pathspec: str) -> bool:
    """Does this pathspec name the top of the tree: `.`, `./`, `./.`, `*`, `:/`, `:(top)` and `:(top).`, or only `..`?"""
    magic = _MAGIC.fullmatch(pathspec)
    if magic:
        return "top" in magic.group(1).split(",") and _tree_name(magic.group(2))
    if pathspec.startswith(":/"):
        return _tree_name(pathspec[2:])
    if not pathspec:
        return False
    return pathspec == ":" or _tree_name(pathspec) or set(posixpath.normpath(pathspec).split("/")) == {".."}


def _pathspec_problem(operands: list[str], sub: str) -> str | None:
    if any(_whole_tree(o) for o in operands):
        return f"git {sub} of the whole tree stages everything"
    if len(operands) == 1 and _EXPANSION.fullmatch(operands[0]):
        return f"git {sub} of {operands[0]} (a variable or substitution) may be the whole tree; name the files"
    return None


def _alias_key(key: str) -> bool:
    """Is this config key an alias? Section names are not case-sensitive (`Alias.x`, `ALIAS.x`)."""
    return key.lower().startswith("alias.")


def _alias_option(options: list[str]) -> bool:
    """Do git's global options define an alias (`-c alias.x=...`, `-calias.x=...`, `--config-env=alias.x=VAR`)?"""
    for i, w in enumerate(options):
        value = options[i + 1] if i + 1 < len(options) else ""
        if w in ("-c", "--config-env") and _alias_key(value):
            return True
        if w.startswith(("-c", "--config-env=")) and _alias_key(w.removeprefix("--config-env=").removeprefix("-c")):
            return True
    return False


def _alias_env(assignments: list[str]) -> bool:
    """Do these `VAR=value` words put an alias into git's config through its environment? `GIT_CONFIG_PARAMETERS`
    is stopped whatever it holds; `GIT_CONFIG_KEY_n` when it names an alias or is a variable or substitution."""
    for a in assignments:
        if not _GIT_ENV.match(a):
            continue
        name, _, value = a.partition("=")
        if name.upper() == "GIT_CONFIG_PARAMETERS" or _alias_key(value) or "$" in value or "`" in value:
            return True
    return False


def _stages_everything(words: list[str]) -> str | None:
    sub = words[1] if len(words) > 1 else ""
    rest = words[2:]
    if sub in _STAGES:
        operands = _operands(rest)
        for w in rest:
            if w == "--":
                break
            if _all_option(w) or (w.startswith("-") and not w.startswith("--") and "A" in w):
                return f"git {sub} -A stages everything"
            if _update_option(w) and not operands and not any(x.startswith("--pathspec-from-file") for x in rest):
                return f"git {sub} -u with no pathspec stages every tracked change"
        return _pathspec_problem(operands, sub)
    if sub == "commit":
        skip = after = False
        paths: list[str] = []
        for w in rest:
            if after:
                paths.append(w)
            elif skip:
                skip = False
            elif w == "--":
                after = True
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
            else:
                paths.append(w)
        return _pathspec_problem(paths, sub)
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
    for words, env in policy.simple_commands_env(call.value, git_options=True):
        why = None
        if words and words[0] in _SETS_VARIABLES:
            env = [*env, *words[1:]]
        if _alias_env(env):
            why = (
                "git's config can be set from the environment (GIT_CONFIG_*) to define a command that stages everything"
            )
        if words and words[0] == "git":
            options, words = policy.git_split(words)
            if _alias_option(options):
                why = "git -c alias.NAME=... can define a command that stages everything"
            why = why or _stages_everything(words)
        if why:
            return Finding("deny", f"{why}; name the files: `git add path ...`, then `git commit -m ...`")
    return None
