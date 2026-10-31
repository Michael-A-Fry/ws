"""Notes module: `ws note` drops a Markdown file into the `_inbox` of your own notes folder (any app reads it).

ws only adds files here; it never edits or deletes what is already in your notes. No git: the folder is yours.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from ws import frontmatter
from ws.profile import ProfileError, load, profile_path


class WsError(Exception):
    """A problem the person can fix; the message is safe to print."""


def role_path(role: str, env: dict | None = None) -> Path:
    """The folder named in [roles.<role>] of the profile."""
    pf = profile_path(env)
    try:
        prof = load(pf) or {}
    except ProfileError as e:
        raise WsError(str(e)) from e
    roles = prof.get("roles")
    cfg = roles.get(role) if isinstance(roles, dict) else None
    p = cfg.get("path") if isinstance(cfg, dict) else None
    if not isinstance(p, str) or not p:
        raise WsError(f"no [roles.{role}] path in {pf} (run `ws doctor`)")
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
    raise WsError(f"too many files called {stem} in {directory}")


def capture(
    text: str,
    root: Path,
    title: str | None = None,
    tags: list[str] | None = None,
    source: str = "cli",
    now: datetime | None = None,
) -> Path:
    """Write one note into root/_inbox and return its path."""
    text = text.strip()
    if not text:
        raise WsError("nothing to note: the text is empty")
    if not root.is_dir():
        raise WsError(f"{root} is not a folder (run `ws init --create-notes`)")
    now = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    meta: dict = {"id": str(uuid.uuid4()), "created": now.replace(microsecond=0), "type": "note"}
    if title and title.strip():
        meta["title"] = title.strip()
    meta.update({"source": source, "tags": clean_tags(tags)})
    stem = f"{now:%Y-%m-%d-%H%M%S}-{title_slug(title, text)}"
    try:
        return write_new(root / "_inbox", stem, frontmatter.dumps(meta, text))
    except OSError as e:
        raise WsError(f"could not write to {root / '_inbox'}: {e.strerror or type(e).__name__}") from e


def read_text(parts: list[str]) -> str:
    """The text from the command line, or from stdin when it is a lone `-`."""
    if parts == ["-"]:
        return sys.stdin.read()
    return " ".join(parts)


def cmd_note(args: argparse.Namespace) -> int:
    try:
        if not args.text:
            raise WsError("nothing to note: give some text, or `-` to read it from stdin")
        path = capture(read_text(args.text), role_path("notes"), args.title, args.tag)
    except WsError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def register(sub) -> None:
    n = sub.add_parser("note", help="capture a note into the _inbox of your notes folder")
    n.add_argument("text", nargs="*", help="the note, or `-` to read it from stdin")
    n.add_argument("--title", help="a title (also names the file)")
    n.add_argument("--tag", action="append", metavar="TAG", help="a tag; repeat for more")
    n.set_defaults(func=cmd_note)
