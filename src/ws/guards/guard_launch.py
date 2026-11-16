"""guard-launch: an isolated agent or a workflow starts only from the main checkout of a git repository.

The folder the session is in decides where an isolated agent's worktree, and a workflow's fixes, land. Started from a
folder with no repository, or from a linked worktree (a worktree inside a worktree), the work lands somewhere nobody
looks. So a Workflow call, and an Agent or Task call that is isolated, is denied unless the session is in the main
checkout of a git repository. An agent is isolated when the call says `isolation: "worktree"`, when the agent type's
own definition does (`isolation: worktree` in the front matter of an agent file under `.claude/agents` in the session's
folder or any folder above it, or in `~/.claude/agents`), or when no definition of the type can be found and it isn't a
built-in one (it could be one the session just wrote). Claude Code matches an agent by the `name:` in its front
matter, not by its file name, so every file in those folders is read, and a file whose front matter can't be read
counts as a definition of every type (any doubt is isolated). A definition is looked at before the built-in names, in
case one overrides a built-in. An agent that asks for no isolation, a fork and a remote
agent are not affected. The person can approve the launch once.

A folder is a main checkout when git says so: `git rev-parse --git-dir` and `--git-common-dir` name the same folder
(a linked worktree's differ), and the top folder is not under `.claude/worktrees`. A planted `.git` folder is not a
repository, and a linked worktree whose `.git` file was rewritten to name the main repository is not its top folder's
own repository, so neither counts. What git can't tell apart: a folder where the model ran `git init` is the main
checkout of a new repository. The sandbox's write-deny list, not this guard, stops a folder being swapped for a link
after the check. Both folders have to be one: the folder the session started in (which the model
can't change) and the folder the call says it runs in. An empty or relative folder, a folder git can't read and
git that doesn't answer in time are a deny, and so is an error in the guard (the gate fails closed on a call before it
runs).

No NATIVE_DENY: what it denies depends on the folder the session is in, which a pattern on a call can't see.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ws.guards import Finding, _git, known_keys

NAME = "guard-launch"
EVENTS = ("pre",)
KINDS = ("agent",)
PRIORITY = 10

BUILT_IN_AGENTS = frozenset(
    {"", "general-purpose", "explore", "plan", "claude", "claude-code-guide", "statusline-setup", "output-style-setup"}
)
_NAME_SHAPE = re.compile(r"[\w][\w.-]{0,80}")
_ISOLATION = re.compile(r"^isolation\s*:\s*[\"']?(\w+)[\"']?\s*$", re.MULTILINE)


def config(table: dict, where: str) -> None:
    known_keys(table, where, ())


def is_main_checkout(folder: str) -> bool:
    """True when `folder` is inside the main checkout of a git repository, as git says."""
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return False
    got = _git.lines(folder, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if got is None or len(got) != 3:
        return False
    git_dir, common, top = (os.path.realpath(x) for x in got)
    own = git_dir == os.path.join(top, ".git")  # a rewritten `.git` file points at another repository's folder
    return git_dir == common and own and "/.claude/worktrees/" not in top + "/"


def _start_folder() -> str:
    from ws.gate import _start_folder as start

    return start()


MAX_AGENT_FILES = 400
MAX_AGENT_BYTES = 65536
_YAML_VALUE = r"\s*:\s*(?:!!\w+\s+)?[>|]?[-+]?\s*[\"']?"  # an optional tag, folded or literal marker, and quote
_NAME_LINE = re.compile(r"^[\"']?name[\"']?" + _YAML_VALUE + r"([^\"'#\s]+)", re.MULTILINE)
_WORKTREE_LINE = re.compile(r"^[\"']?isolation[\"']?" + _YAML_VALUE + r"worktree\b", re.MULTILINE | re.IGNORECASE)
_FRONT_END = re.compile(r"^---\s*$", re.MULTILINE)


def _agent_folders(folder: str) -> list[Path]:
    """`.claude/agents` of `folder` and each folder above it, then `~/.claude/agents`."""
    here = Path(os.path.realpath(folder)) if folder and os.path.isabs(folder) else None
    places = [d / ".claude" / "agents" for d in ([here, *here.parents] if here else [])]
    places.append(Path(os.path.expanduser("~")) / ".claude" / "agents")
    return places


def _definitions(name: str, folder: str) -> tuple[bool, bool]:
    """(some agent file defines `name`, some such file asks for a worktree). An agent file defines the type its
    front matter's `name:` says; one with no readable `name:` counts as defining every type, and one that asks for a
    worktree anywhere in its head counts as asking, so a comment, a stray `---` or an odd layout never hides it."""
    found = asks = False
    for place in _agent_folders(folder):
        try:
            files = sorted(place.glob("*.md"))[:MAX_AGENT_FILES]
        except OSError:
            continue
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(MAX_AGENT_BYTES)
            except OSError:
                continue
            named = _NAME_LINE.search(head) if head.startswith("---") else None
            if named and named.group(1) != name:
                continue
            found = True
            # Any doubt is "asks": the key in any YAML spelling, `worktree` anywhere in the front matter, or no closing
            # `---` within what was read (the front matter is longer than we looked at).
            close = _FRONT_END.search(head, 3) if head.startswith("---") else None
            front = head[: close.start()] if close else head
            asks = asks or bool(
                _WORKTREE_LINE.search(head) or "worktree" in front.lower() or (head.startswith("---") and not close)
            )
    return found, asks


def _isolated(data, folders: list[str]) -> str:
    """Why this call is isolated, or "" when it isn't."""
    if data.get("tool") == "Workflow":
        return "a workflow"
    if data.get("isolation") == "worktree":
        return "an isolated agent"
    kind = (data.get("subagent_type") or "").strip()
    if not _NAME_SHAPE.fullmatch(kind or "x"):
        return f"the agent type {kind[:40]!r}, which isn't a name that can be looked up,"
    found = asks = False
    for folder in folders:
        f, a = _definitions(kind, folder)
        found, asks = found or f, asks or a
    if asks:
        return f"the agent type {kind!r}, whose definition asks for a worktree,"
    if found or kind.lower() in BUILT_IN_AGENTS:
        return ""
    return f"the agent type {kind!r}, which has no definition to read,"


def judge(call, cfg, ctx) -> Finding | None:
    data = call.data or {}
    folders = [ctx.cwd, _start_folder()]
    what = _isolated(data, folders)
    if not what:
        return None
    if all(is_main_checkout(f) for f in folders):
        return None
    return Finding(
        "deny",
        f"guard-launch: {what} starts only from the main checkout of a git repository, and this session is in "
        f"{ctx.cwd or 'no folder'}, which is not one (no repository, a linked worktree, or a folder git can't read). "
        "Start the session in the main checkout, or run the work without isolation.",
        approvable=True,
    )
