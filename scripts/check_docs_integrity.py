#!/usr/bin/env python3
"""Docs integrity: the policy files exist, every local link, image and anchor in the tracked Markdown resolves, and no
banned string is in it.

Adapted from claw-code's .github/scripts/check_release_readiness.py (https://github.com/ultraworkers/claw-code),
Copyright (c) 2026 UltraWorkers and Claw Code contributors, MIT License. The checks are changed to fit ws.

    check_docs_integrity.py [--allow-missing FILE] [--root DIR]

--allow-missing names a file of paths (one per line, # comments allowed) that may not exist yet: a link to one, or a
required file that is one, is not an error. While the docs are written on parallel branches this lists the pages
others link to; once they have all landed the file is empty. Stdlib only. Exit 0 clean, 1 problems, 2 usage.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "NOTICE",
    "CLA.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
]

# Fenced blocks are code, not prose: links in them are examples. Banned strings are still checked there.
FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,}).*?^(?P=fence)[ \t]*$", re.DOTALL | re.MULTILINE)
INLINE_CODE = re.compile(r"`[^`\n]*`")
INLINE_LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE_DEF = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.MULTILINE)
HTML_LINK = re.compile(r"""<(?:a|img)\b[^>]*?(?:href|src)=["']([^"']+)["']""", re.IGNORECASE)
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")

# The old name only counts as banned where someone could install it; the history may still mention it.
BANNED = [
    (
        re.compile(r"\b(?:pipx|pip3?|uv)\s+(?:tool\s+|pip\s+)?install\b[^\n]*\bws-toolkit\b"),
        "the old name ws-toolkit as an install target",
    ),
    (
        re.compile(r"build[- ]from[- ]source only", re.IGNORECASE),
        "'build-from-source only' (there is an installer and a package)",
    ),
    (re.compile(r"\b[a-z0-9-]+\.(?:ts\.net|home\.arpa|lan|internal)\b", re.IGNORECASE), "a private host name"),
]
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d|/\d)")
PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
]
SKIP_DIRS = {".git", ".venv", "node_modules", ".claude", ".claude-state", ".pytest_cache", ".ruff_cache", "dist"}


def github_anchor(heading: str) -> str:
    """The id GitHub gives a heading: lower case, markup and punctuation dropped, spaces to hyphens."""
    text = re.sub(r"<[^>]+>", "", heading.strip().lower())
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_for(text: str) -> set[str]:
    """Every anchor a Markdown file offers. A repeated heading gets -1, -2 ... as on GitHub."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in FENCE.sub("", text).splitlines():
        m = HEADING.match(line)
        if not m:
            continue
        base = github_anchor(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        anchors.add(base if n == 0 else f"{base}-{n}")
    return anchors


def markdown_files(root: Path) -> list[Path]:
    """Tracked and new (not ignored) *.md files; outside a Git work tree, every *.md except tool and state folders."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        files = [root / p for p in out.split("\0") if p]
        if files:
            return sorted(f for f in files if f.is_file())
    except (OSError, subprocess.CalledProcessError):
        pass
    return sorted(p for p in root.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(root).parts))


def read_pending(path: Path | None) -> set[str]:
    if path is None:
        return set()
    lines = (ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return {ln.rstrip("/") for ln in lines if ln}


def link_targets(text: str) -> list[str]:
    prose = INLINE_CODE.sub("", FENCE.sub("", text))
    found = [m.group(1) for m in INLINE_LINK.finditer(prose)]
    found += [m.group(1) for m in REFERENCE_DEF.finditer(prose)]
    found += [m.group(1) for m in HTML_LINK.finditer(prose)]
    return found


def is_external(target: str) -> bool:
    return urlparse(target).scheme != "" or target.startswith("//")


def check_required(root: Path, pending: set[str], errors: list[str]) -> None:
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file() and rel not in pending:
            errors.append(f"missing required policy file: {rel}")


def check_links(root: Path, files: list[Path], pending: set[str], errors: list[str]) -> None:
    anchor_cache: dict[Path, set[str]] = {}
    for path in files:
        rel_src = path.relative_to(root)
        for target in link_targets(path.read_text(encoding="utf-8")):
            if not target or is_external(target):
                continue
            link_path, _, raw_anchor = target.partition("#")
            if link_path.startswith("/"):
                dest = (root / unquote(link_path).lstrip("/")).resolve()
            else:
                dest = (path.parent / unquote(link_path)).resolve() if link_path else path.resolve()
            try:
                rel_dest = dest.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{rel_src}: link leaves the repository: {target}")
                continue
            if not dest.exists():
                if rel_dest.as_posix().rstrip("/") not in pending:
                    errors.append(f"{rel_src}: missing link target: {target}")
                continue
            if raw_anchor and dest.suffix.lower() == ".md":
                if dest not in anchor_cache:
                    anchor_cache[dest] = anchors_for(dest.read_text(encoding="utf-8"))
                if unquote(raw_anchor).lower() not in anchor_cache[dest]:
                    errors.append(f"{rel_src}: missing anchor #{raw_anchor} in {rel_dest}")


def is_private_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(ip in net for net in PRIVATE_NETS)


def check_banned(root: Path, files: list[Path], errors: list[str]) -> None:
    for path in files:
        rel = path.relative_to(root)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern, what in BANNED:
                if pattern.search(line):
                    errors.append(f"{rel}:{n}: {what}")
            if any(is_private_ip(m.group(0)) for m in IPV4.finditer(line)):
                errors.append(f"{rel}:{n}: a private IP address")


def run(root: Path, pending: set[str]) -> list[str]:
    errors: list[str] = []
    files = markdown_files(root)
    check_required(root, pending, errors)
    check_links(root, files, pending, errors)
    check_banned(root, files, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_docs_integrity.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--allow-missing", type=Path, metavar="FILE", help="paths that may not exist yet, one per line")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if args.allow_missing is not None and not args.allow_missing.is_file():
        print(f"check_docs_integrity: no such file: {args.allow_missing}", file=sys.stderr)
        return 2
    pending = read_pending(args.allow_missing)
    errors = run(root, pending)
    for rel in sorted(p for p in pending if (root / p).exists()):
        print(f"note: {rel} now exists; remove it from {args.allow_missing}")
    if errors:
        print("docs integrity check failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"docs integrity check passed ({len(markdown_files(root))} Markdown files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
