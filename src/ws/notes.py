"""Notes module: `ws note` drops a Markdown file into the `_inbox` of your own notes folder (any app reads it).

ws only adds files here; it never edits or deletes what is already in your notes. No git: the folder is yours.

Walled classes. The notes and memory folders are shared stores, so a session whose class is walled (or counts-only) does
not write to them: `capture` and `memory.add` refuse it with a typed `refused` error (`refuse_walled`), because a note
or a memory file would carry the class's content out of its repo. The class is `policy.class_of` of the process's real
working folder, never an argument; with no `[classes]` in the policy nothing changes, and a policy that will not load
refuses (the class can't be told). A folder in no class is not a walled class and is not refused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from ws import frontmatter, output, policy
from ws.output import WsError  # re-exported: memory and the MCP server import it from here
from ws.profile import ProfileError, load, profile_path


def load_policy(env: dict | None = None) -> policy.Policy:
    """The policy at the config folder of `env` (the process's own when None). One that will not load is a `refused`
    error: with it unread, the data class of the caller can't be told."""
    try:
        return policy.load(policy.policy_path(env))
    except (policy.PolicyError, OSError, ValueError) as e:
        raise WsError(
            "refused",
            "ws can't read its policy, so it can't tell which data class this session is in",
            "fix policy.toml (`ws doctor` says where)",
        ) from e


def caller_class(pol: policy.Policy, cwd: str | None = None) -> policy.Class | None:
    """The data class of the caller: `policy.class_of` of the real working folder of this process (`cwd` is for callers
    that already hold it). None when the policy has no `[classes]`. A working folder that can't be read is a `refused`
    error, never a guess."""
    if not pol.classes:
        return None
    try:
        here = os.getcwd() if cwd is None else cwd
    except OSError as e:
        raise WsError(
            "refused",
            f"the current folder can't be read ({e.strerror}), so the session's data class is not known",
            "run it from a project folder",
        ) from e
    return policy.class_of(pol, here)


def is_walled(cls: policy.Class | None) -> bool:
    """True for a class a session must not carry content out of: a declared class that is not `full`. A folder in no
    class (UNCLASSIFIED) is not a class of anyone's, so it is not one."""
    return cls is not None and cls is not policy.UNCLASSIFIED and cls.access != "full"


def refuse_walled(what: str, env: dict | None = None, *, cwd: str | None = None) -> None:
    """Raise `refused` when the caller's class is walled: `what` ("add a memory file", "capture a note") would put the
    class's content in a store every class can read."""
    cls = caller_class(load_policy(env), cwd)
    if cls is not None and is_walled(cls):
        raise WsError(
            "refused",
            f"a session in the walled class {cls.name!r} can't {what}: the shared store would carry its content out",
            "keep it in the class's own repo, or leave it out; a walled class's files stay inside the class",
        )


def role_path(role: str, env: dict | None = None) -> Path:
    """The folder named in [roles.<role>] of the profile."""
    pf = profile_path(env)
    try:
        prof = load(pf) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {pf}") from e
    roles = prof.get("roles")
    cfg = roles.get(role) if isinstance(roles, dict) else None
    p = cfg.get("path") if isinstance(cfg, dict) else None
    if not isinstance(p, str) or not p:
        raise WsError("not_found", f"no [roles.{role}] path in {pf}", "run `ws doctor`")
    return Path(p).expanduser().resolve()  # absolute, so index keys do not depend on the current directory


def slugify(text: str, limit: int = 48) -> str:
    """A short, safe file-name stem: lower-case ASCII words joined by hyphens. 'note' if nothing is left."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(words) > limit:
        words = words[:limit].rsplit("-", 1)[0] if "-" in words[:limit] else words[:limit]
    return words.strip("-") or "note"


def title_slug(title: str | None, text: str) -> str:
    """Slug from the title if there is one, else from the first words of the text."""
    if title and title.strip():
        return slugify(title)
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return slugify(" ".join(first.split()[:8]))


def clean_tags(tags: list[str] | None) -> list[str]:
    return [t.strip() for t in (tags or []) if isinstance(t, str) and t.strip()]


def write_new(directory: Path, stem: str, content: str) -> Path:
    """Write content to directory/stem.md without overwriting: on a clash try stem-2, stem-3, ..."""
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        path = directory / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except FileExistsError:
            continue
        return path
    raise WsError("failed", f"too many files called {stem} in {directory}")


def capture(
    text: str,
    root: Path,
    title: str | None = None,
    tags: list[str] | None = None,
    source: str = "cli",
    now: datetime | None = None,
    env: dict | None = None,
) -> Path:
    """Write one note into root/_inbox and return its path. Refused for a session in a walled class (`refuse_walled`)."""
    refuse_walled("capture a note", env)
    text = text.strip()
    if not text:
        raise WsError("bad_input", "nothing to note: the text is empty", "give some text, or `-` to read it from stdin")
    if not root.is_dir():
        raise WsError("not_found", f"{root} is not a folder", "run `ws init --create-notes`")
    now = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    meta: dict = {"id": str(uuid.uuid4()), "created": now.replace(microsecond=0), "type": "note"}
    if title and title.strip():
        meta["title"] = title.strip()
    meta.update({"source": source, "tags": clean_tags(tags)})
    stem = f"{now:%Y-%m-%d-%H%M%S}-{title_slug(title, text)}"
    try:
        return write_new(root / "_inbox", stem, frontmatter.dumps(meta, text))
    except OSError as e:
        raise WsError("io", f"could not write to {root / '_inbox'}: {e.strerror or type(e).__name__}") from e


def read_text(parts: list[str]) -> str:
    """The text from the command line, or from stdin when it is a lone `-`."""
    if parts == ["-"]:
        return sys.stdin.read()
    return " ".join(parts)


def cmd_note(args: argparse.Namespace) -> int:
    if not args.text:
        raise WsError("bad_input", "nothing to note: give some text", "or `-` to read it from stdin")
    path = capture(read_text(args.text), role_path("notes"), args.title, args.tag)
    if args.json:
        output.result({"path": str(path)})
        return 0
    print(path)
    return 0


def register(sub) -> None:
    n = sub.add_parser("note", help="capture a note into the _inbox of your notes folder")
    n.add_argument("text", nargs="*", help="the note, or `-` to read it from stdin")
    n.add_argument("--title", help="a title (also names the file)")
    n.add_argument("--tag", action="append", metavar="TAG", help="a tag; repeat for more")
    n.set_defaults(func=cmd_note)
