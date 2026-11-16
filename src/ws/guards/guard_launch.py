"""guard-launch: an isolated agent or a workflow starts only from the main checkout of a git repository.

The folder the session is in decides where an isolated agent's worktree, and a workflow's fixes, land. Started from a
folder with no repository, or from a linked worktree (a worktree inside a worktree), the work lands somewhere nobody
looks. So a Workflow call, and an Agent or Task call that is isolated, is denied unless the session is in the main
checkout of a git repository. An agent is isolated when the call says `isolation: "worktree"`, when the agent type's
own definition does (`isolation: worktree` in the front matter of `.claude/agents/NAME.md`, in the session's folder or
any folder above it, or of `~/.claude/agents/NAME.md`), or when the type is not a built-in one and no definition of
it can be found (it could be one the session just wrote). An agent that asks for no isolation, a fork and a remote
agent are not affected. The person can approve the launch once.

A folder is a main checkout when git says so: `git rev-parse --git-dir` and `--git-common-dir` name the same folder
(a linked worktree's differ), and the top folder is not under `.claude/worktrees`. A planted `.git` folder is not a
repository, so it does not count. Both folders have to be one: the folder the session started in (which the model
can't change) and the folder the call says it runs in. An empty or relative folder, a folder git can't read and
git that doesn't answer in time are a deny, and so is an error in the guard (the gate fails closed on a call before it
runs).

No NATIVE_DENY: what it denies depends on the folder the session is in, which a pattern on a call can't see.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ws.guards import Finding, known_keys

NAME = "guard-launch"
EVENTS = ("pre",)
KINDS = ("agent",)
PRIORITY = 10

GIT_SECONDS = 5
BUILT_IN_AGENTS = frozenset(
    {"", "general-purpose", "explore", "plan", "claude", "claude-code-guide", "statusline-setup", "output-style-setup"}
)
_NAME_SHAPE = re.compile(r"[\w][\w.-]{0,80}")
_ISOLATION = re.compile(r"^isolation\s*:\s*[\"']?(\w+)[\"']?\s*$", re.MULTILINE)


def config(table: dict, where: str) -> None:
    known_keys(table, where, ())


def _git_env() -> dict[str, str]:
    drop = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
    return {**{k: v for k, v in os.environ.items() if k not in drop}, "GIT_NO_LAZY_FETCH": "1"}


def _git(folder: str, *args: str) -> list[str] | None:
    """The lines `git -C folder args` prints, or None when it fails, isn't there or takes too long."""
    try:
        done = subprocess.run(
            ["git", "-C", folder, *args],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def is_main_checkout(folder: str) -> bool:
    """True when `folder` is inside the main checkout of a git repository, as git says."""
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return False
    got = _git(folder, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if got is None or len(got) != 3:
        return False
    git_dir, common, top = (os.path.realpath(x) for x in got)
    return git_dir == common and "/.claude/worktrees/" not in top + "/"


def _start_folder() -> str:
    from ws.gate import _start_folder as start

    return start()


def _definition_isolation(name: str, folder: str) -> str | None:
    """`isolation` in the front matter of the agent's definition (`worktree` or other), "" when the definition has
    none, and None when no definition is found. Looks in `.claude/agents` of the folder and each one above it, then in
    `~/.claude/agents`."""
    if not _NAME_SHAPE.fullmatch(name):
        return None
    here = Path(os.path.realpath(folder)) if folder and os.path.isabs(folder) else None
    places = [d / ".claude" / "agents" for d in ([here, *here.parents] if here else [])]
    places.append(Path(os.path.expanduser("~")) / ".claude" / "agents")
    for place in places:
        try:
            text = (place / f"{name}.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = text.split("---", 2)
        found = _ISOLATION.search(head[1]) if text.startswith("---") and len(head) >= 3 else None
        return found.group(1).lower() if found else ""
    return None


def _isolated(data, folders: list[str]) -> str:
    """Why this call is isolated, or "" when it isn't."""
    if data.get("tool") == "Workflow":
        return "a workflow"
    if data.get("isolation") == "worktree":
        return "an isolated agent"
    kind = (data.get("subagent_type") or "").strip()
    if kind.lower() in BUILT_IN_AGENTS:
        return ""
    found = [_definition_isolation(kind, f) for f in folders]
    if "worktree" in found:
        return f"the agent type {kind!r}, whose definition asks for a worktree,"
    if any(f is not None for f in found):
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
