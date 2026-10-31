#!/usr/bin/env python3
"""Check that the Markdown in the repo holds together: policy files, links, anchors and banned strings.

Usage: check_docs_integrity.py [--allow-missing FILE] [--root DIR]

--allow-missing names a text file of repo-relative paths, one per line (`#` starts a comment). Those paths may not exist
yet: a link to one, or a required file that is one, is not an error. A path that now exists is reported as a note so
the line can be removed.

Exit codes: 0 clean, 1 problems found, 2 usage (a bad flag, or an --allow-missing file that does not exist).
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "CLA.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
)

# Folders the fallback scan skips when git cannot list the files.
SKIP_PARTS = {".git", ".venv", "node_modules", ".claude", ".claude-state", ".pytest_cache", ".ruff_cache", "dist"}

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")  # one line only: a stray backtick must not hide links far below it
INLINE_LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>]*>|[^)\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*(<[^>]*>|\S+)", re.MULTILINE)
HTML_REF = re.compile(r"<(?:a\s[^>]*?href|img\s[^>]*?src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
MD_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")

OLD_NAME = re.compile(r"\b(?:pipx|pip3?|uv)\s+(?:(?:tool|pip)\s+)?install\b.*?\bws-toolkit\b")
BUILD_ONLY = re.compile(r"build[ -]from[ -]source only", re.IGNORECASE)
PRIVATE_HOST = re.compile(r"\.(?:ts\.net|home\.arpa|lan|internal)\b", re.IGNORECASE)
DOTTED_QUAD = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.\d)(?!/\d)")
PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
]


def _is_closing_fence(match: re.Match[str] | None, marker: str) -> bool:
    """True when `match` is a fence line that closes a block opened with `marker`."""
    return bool(match) and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker)


def strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans, so links inside them are not read."""
    kept: list[str] = []
    marker = ""
    for line in text.splitlines():
        fence = FENCE.match(line)
        if marker:
            if _is_closing_fence(fence, marker):
                marker = ""
            continue
        if fence:
            marker = fence.group(1)
            continue
        kept.append(CODE_SPAN.sub("", line))
    return "\n".join(kept)


def link_targets(text: str) -> list[str]:
    """Every link, image, reference definition and HTML href or src in `text`, outside code."""
    body = strip_code(text)
    found = [m.group(1) for m in INLINE_LINK.finditer(body)]
    found += [m.group(1) for m in REF_DEF.finditer(body)]
    found += [m.group(1) or m.group(2) for m in HTML_REF.finditer(body)]
    return [t[1:-1] if t.startswith("<") and t.endswith(">") else t for t in found]


def slug(heading: str) -> str:
    """The GitHub anchor for a heading's text, before any repeat suffix."""
    s = HTML_TAG.sub("", heading)
    s = MD_LINK_TEXT.sub(r"\1", s).lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", s).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor a Markdown file offers; a repeated heading gets -1, -2 and so on."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    marker = ""
    for line in text.splitlines():
        fence = FENCE.match(line)
        if marker:
            if _is_closing_fence(fence, marker):
                marker = ""
            continue
        if fence:
            marker = fence.group(1)
            continue
        heading = HEADING.match(line)
        if not heading:
            continue
        base = slug(heading.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.add(base if count == 0 else f"{base}-{count}")
    return out


def markdown_files(root: Path) -> list[Path]:
    """Tracked and new-but-not-ignored Markdown files; every *.md under root if git can't say."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        names = [n for n in proc.stdout.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
    files = sorted(p for p in (root / n for n in names) if p.is_file())
    if files:
        return files
    return sorted(p for p in root.rglob("*.md") if not SKIP_PARTS & set(p.relative_to(root).parts))


def check_links(root: Path, files: list[Path], pending: set[str]) -> list[str]:
    """Missing or escaping link targets and missing anchors, one message each."""
    errors: list[str] = []
    cache: dict[Path, set[str]] = {}
    for src in files:
        rel_src = src.relative_to(root).as_posix()
        for target in link_targets(src.read_text(encoding="utf-8")):
            if not target or SCHEME.match(target):
                continue
            path_part, _, frag = target.partition("#")
            if not path_part:
                dest = src
            else:
                base = root if path_part.startswith("/") else src.parent
                dest = (base / unquote(path_part).lstrip("/")).resolve()
            if not dest.is_relative_to(root):
                errors.append(f"{rel_src}: link leaves the repository: {target}")
                continue
            if not dest.exists():
                if dest.relative_to(root).as_posix().rstrip("/") not in pending:
                    errors.append(f"{rel_src}: missing link target: {target}")
                continue
            if frag and dest.suffix == ".md" and dest.is_file():
                if dest not in cache:
                    cache[dest] = anchors(dest.read_text(encoding="utf-8"))
                if unquote(frag).lower() not in cache[dest]:
                    errors.append(f"{rel_src}: missing anchor #{frag} in {dest.relative_to(root).as_posix()}")
    return errors


def has_private_ip(line: str) -> bool:
    """True when `line` holds a dotted-quad address inside a private or link-local range."""
    for m in DOTTED_QUAD.finditer(line):
        try:
            addr = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if any(addr in net for net in PRIVATE_NETS):
            return True
    return False


def check_banned(root: Path, files: list[Path]) -> list[str]:
    """Strings that must not appear anywhere in the docs, as `path:line: reason`."""
    rules = (
        (lambda s: bool(OLD_NAME.search(s)), "the old name ws-toolkit as an install target"),
        (lambda s: bool(BUILD_ONLY.search(s)), "'build-from-source only' (there is an installer and a package)"),
        (lambda s: bool(PRIVATE_HOST.search(s)), "a private host name"),
        (has_private_ip, "a private IP address"),
    )
    errors: list[str] = []
    for src in files:
        rel = src.relative_to(root).as_posix()
        for number, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            errors += [f"{rel}:{number}: {why}" for hit, why in rules if hit(line)]
    return errors


def run(root: Path, pending: set[str]) -> list[str]:
    """All problems under `root`: required files first, then links, then banned strings."""
    errors = [
        f"missing required policy file: {name}"
        for name in REQUIRED_FILES
        if not (root / name).exists() and name not in pending
    ]
    files = markdown_files(root)
    errors += check_links(root, files, pending)
    errors += check_banned(root, files)
    return errors


def read_pending(path: Path) -> set[str]:
    """The paths in an --allow-missing file: comments, blanks and trailing slashes dropped."""
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            out.add(entry.rstrip("/"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--allow-missing", metavar="FILE", help="file of repo-relative paths that may not exist yet")
    parser.add_argument(
        "--root", default=str(Path(__file__).resolve().parents[1]), help="repo root (default: this repo)"
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    pending: set[str] = set()
    if args.allow_missing:
        pending_file = Path(args.allow_missing)
        if not pending_file.is_file():
            print(f"check_docs_integrity: no such file: {args.allow_missing}", file=sys.stderr)
            return 2
        pending = read_pending(pending_file)
        for entry in sorted(pending):
            if (root / entry).exists():
                print(f"note: {entry} now exists; remove it from {args.allow_missing}")
    errors = run(root, pending)
    if errors:
        print("docs integrity check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"docs integrity check passed ({len(markdown_files(root))} Markdown files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
