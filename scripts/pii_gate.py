#!/usr/bin/env python3
"""PII gate: fail when a private IP, a tailnet host, an e-mail address or a denylisted term reaches the repo.

    pii_gate.py [FILE ...]      check these files (prek passes the staged ones)
    pii_gate.py --tracked       check every tracked file
    pii_gate.py --history       check every added line and commit message reachable from HEAD

The denylist is private and never committed: one term per line (# comments allowed), read from the environment
variable WS_PII_DENYLIST (CI: a repo secret) or the file named by WS_PII_DENYLIST_FILE (default
~/.config/ws/pii-denylist). With no denylist the gate fails, unless WS_PII_DENYLIST_OPTIONAL=1 (fork pull requests,
which get no secrets). A hit names the file, line and rule, never the matched text: CI logs are public.

False positives go in .pii-allow (one Python regex per line); a line matching any of them is skipped.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
]
TAILNET = re.compile(r"\b[a-z0-9-]+\.ts\.net\b|\btail[0-9a-f]{6}\b", re.IGNORECASE)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(
    r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$|^(?:noreply|security)@", re.IGNORECASE
)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".gz"}


def load_denylist() -> list[re.Pattern] | None:
    text = os.environ.get("WS_PII_DENYLIST")
    if not text:
        f = Path(os.environ.get("WS_PII_DENYLIST_FILE", Path.home() / ".config/ws/pii-denylist"))
        text = f.read_text() if f.is_file() else None
    if text is None:
        return None
    terms = [t.strip() for t in text.splitlines() if t.strip() and not t.strip().startswith("#")]
    return [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.IGNORECASE) for t in terms]


def load_allow(root: Path) -> list[re.Pattern]:
    f = root / ".pii-allow"
    if not f.is_file():
        return []
    return [re.compile(x.strip()) for x in f.read_text().splitlines() if x.strip() and not x.startswith("#")]


def line_hits(line: str, deny: list[re.Pattern]) -> list[str]:
    hits = []
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.ip_address(m.group())
        except ValueError:
            continue
        if any(ip in n for n in PRIVATE_NETS):
            hits.append("private IP")
    if TAILNET.search(line):
        hits.append("tailnet host")
    for m in EMAIL.finditer(line):
        if not EMAIL_OK.search(m.group()):
            hits.append("e-mail address")
    for i, p in enumerate(deny, 1):
        if p.search(line):
            hits.append(f"denylist term #{i}")
    return hits


def scan_lines(where: str, lines, deny, allow) -> int:
    n = 0
    for no, line in lines:
        if any(a.search(line) for a in allow):
            continue
        for rule in line_hits(line, deny):
            print(f"{where}:{no}: {rule}")
            n += 1
    return n


def file_lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return enumerate(text.splitlines(), 1)


def history_chunks():
    """Yield (commit, [(n, line)]) for every message and added line reachable from HEAD (other branches are theirs)."""
    out = subprocess.run(
        [
            "git",
            "log",
            "-p",
            "--no-color",
            "--format=@@COMMIT %H%n%B%n@@ENDMSG",
        ],  # %n: a squash-merge message has no final newline
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout
    commit, lines, in_msg = None, [], False
    for raw in out.splitlines():
        if raw.startswith("@@COMMIT "):
            if commit:
                yield commit, lines
            commit, lines, in_msg = raw.split()[1][:12], [], True
        elif raw == "@@ENDMSG":
            in_msg = False
        elif in_msg or (raw.startswith("+") and not raw.startswith("+++")):
            lines.append((len(lines) + 1, raw if in_msg else raw[1:]))
    if commit:
        yield commit, lines


def main(argv: list[str]) -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
        ).stdout.strip()
        or "."
    )
    deny = load_denylist()
    if deny is None:
        if os.environ.get("WS_PII_DENYLIST_OPTIONAL") == "1":
            print("pii-gate: no denylist (fork PR?): checking the built-in rules only", file=sys.stderr)
            deny = []
        else:
            print("pii-gate: no denylist (WS_PII_DENYLIST or WS_PII_DENYLIST_FILE): refusing to pass", file=sys.stderr)
            return 2
    allow = load_allow(root)
    hits = 0
    if argv[:1] == ["--history"]:
        for commit, lines in history_chunks():
            hits += scan_lines(f"commit {commit}", lines, deny, allow)
    else:
        if argv[:1] == ["--tracked"]:
            files = subprocess.run(
                ["git", "ls-files", "-z"], capture_output=True, text=True, cwd=root, check=True
            ).stdout.split("\0")
            paths = [root / f for f in files if f]
        else:
            paths = [Path(a) for a in argv]
        for p in paths:
            if p.suffix.lower() in SKIP_SUFFIXES or not p.is_file():
                continue
            hits += scan_lines(str(p.relative_to(root) if p.is_absolute() else p), file_lines(p), deny, allow)
    if hits:
        print(
            f"pii-gate: {hits} hit(s). Remove them, or add a narrow regex to .pii-allow if one is a false positive.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
