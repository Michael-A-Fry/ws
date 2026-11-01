#!/usr/bin/env python3
"""Make the README read right on PyPI: every relative link and image becomes an absolute URL pinned to the tag.

PyPI shows the README as the project description, but a relative link (`docs/usage.md`, `CHANGELOG.md`) resolves
against pypi.org there and gives a 404. The release workflow runs `rewrite` on its own checkout just before `uv build`,
so the built sdist and wheel carry links to the docs of the version you installed, not whatever `main` holds today.
`check` then reads the description back out of the built files and fails the release on any link or image that is still
relative.

Usage:
  pypi_readme.py rewrite --tag vX.Y.Z [README.md]   rewrite the file in place
  pypi_readme.py check FILE...                      README, METADATA, PKG-INFO, a wheel or an sdist

Links go to `https://github.com/<repo>/blob/<tag>/<path>`, images to `https://raw.githubusercontent.com/<repo>/<tag>/
<path>`, and an anchor on its own (`#install`) to the README on GitHub at the tag, because PyPI drops heading anchors.
Fenced code blocks and inline code are left alone.

Exit codes: 0 clean, 1 relative links found, 2 usage.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

REPO = "Michael-A-Fry/ws"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# `[text](target)` and `![alt](target)`, with an optional "title"; `[label]: target` reference definitions; HTML
# `src="..."` and `href="..."`.
_INLINE = re.compile(r"(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?P<target><[^>]*>|[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
_REFDEF = re.compile(r"^(?P<lead> {0,3}\[[^\]]+\]:[ \t]+)(?P<target>\S+)", re.MULTILINE)
_HTML = re.compile(r"(?P<attr>\b(?:src|href))=(?P<q>[\"'])(?P<target>[^\"']*)(?P=q)", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")


def is_absolute(target: str) -> bool:
    """True for a URL with a scheme (https:, mailto:) or a protocol-relative `//host` one."""
    target = target.strip("<>")
    return bool(_SCHEME.match(target)) or target.startswith("//")


def absolute(target: str, tag: str, *, image: bool) -> str:
    """The absolute URL for one relative target, pinned to `tag`."""
    bracketed = target.startswith("<") and target.endswith(">")
    bare = target.strip("<>")
    if is_absolute(bare):
        return target
    if bare.startswith("#"):
        url = f"https://github.com/{REPO}/blob/{tag}/README.md{bare}"
    else:
        path = bare.removeprefix("./").lstrip("/")
        if image:
            url = f"https://raw.githubusercontent.com/{REPO}/{tag}/{path}"
        else:
            url = f"https://github.com/{REPO}/blob/{tag}/{path}"
    return f"<{url}>" if bracketed else url


def _prose_segments(text: str):
    """Yield (is_prose, chunk) over `text`, a line at a time: lines in fenced code blocks are not prose."""
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = _FENCE.match(line)
        if fence is not None:
            yield False, line
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            yield False, line
            continue
        yield True, line


def _masked(line: str) -> tuple[str, list[str]]:
    """`line` with each inline code span swapped for a placeholder, and the spans to put back."""
    spans: list[str] = []

    def keep(m: re.Match[str]) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    return _CODE_SPAN.sub(keep, line), spans


def _unmasked(line: str, spans: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], line)


def _sub_prose(line: str, tag: str) -> str:
    def inline(m: re.Match[str]) -> str:
        target = absolute(m.group("target"), tag, image=bool(m.group("bang")))
        return f"{m.group('bang')}[{m.group('text')}]({target}{m.group('title') or ''})"

    def html(m: re.Match[str]) -> str:
        image = m.group("attr").lower() == "src"
        return f"{m.group('attr')}={m.group('q')}{absolute(m.group('target'), tag, image=image)}{m.group('q')}"

    text, spans = _masked(line)
    text = _INLINE.sub(inline, text)
    text = _HTML.sub(html, text)
    text = _REFDEF.sub(lambda m: m.group("lead") + absolute(m.group("target"), tag, image=False), text)
    return _unmasked(text, spans)


def rewrite(text: str, tag: str) -> str:
    """`text` with every relative link, image and reference definition outside code made absolute at `tag`."""
    if not TAG_RE.match(tag):
        raise ValueError(f"tag {tag!r} is not vX.Y.Z")
    return "".join(_sub_prose(chunk, tag) if prose else chunk for prose, chunk in _prose_segments(text))


def relative_targets(text: str) -> list[str]:
    """Every link or image target outside code that is not an absolute URL, in order."""
    found: list[str] = []
    for prose, chunk in _prose_segments(text):
        if not prose:
            continue
        chunk, _ = _masked(chunk)
        for rx in (_INLINE, _HTML, _REFDEF):
            found += [m.group("target") for m in rx.finditer(chunk) if not is_absolute(m.group("target"))]
    return found


def description_of(path: Path) -> str:
    """The long description held by a README, a METADATA or PKG-INFO file, a wheel or an sdist."""
    name = path.name
    if name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            meta = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if len(meta) != 1:
                raise ValueError(f"{path}: expected one .dist-info/METADATA, found {len(meta)}")
            raw = zf.read(meta[0]).decode("utf-8")
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as tf:
            info = [
                m for m in tf.getmembers() if m.isfile() and m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
            ]
            if len(info) != 1:
                raise ValueError(f"{path}: expected one top-level PKG-INFO, found {len(info)}")
            fh = tf.extractfile(info[0])
            assert fh is not None
            raw = fh.read().decode("utf-8")
    elif name in ("METADATA", "PKG-INFO"):
        raw = path.read_text(encoding="utf-8")
    else:
        return path.read_text(encoding="utf-8")
    msg = Parser().parsestr(raw)
    body = msg.get_payload()
    if not isinstance(body, str) or not body.strip():
        body = msg.get("Description", "")
    if not body.strip():
        raise ValueError(f"{path}: no long description in the metadata")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rw = sub.add_parser("rewrite", help="rewrite a README in place")
    rw.add_argument("--tag", required=True)
    rw.add_argument("readme", nargs="?", default="README.md", type=Path)
    ck = sub.add_parser("check", help="fail on relative links in built files")
    ck.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "rewrite":
        if not TAG_RE.match(args.tag):
            print(f"pypi_readme: tag {args.tag!r} is not vX.Y.Z", file=sys.stderr)
            return 2
        before = args.readme.read_text(encoding="utf-8")
        after = rewrite(before, args.tag)
        args.readme.write_text(after, encoding="utf-8")
        left = relative_targets(after)
        print(f"pypi_readme: {args.readme} links pinned to {args.tag}; {len(left)} relative left")
        return 1 if left else 0

    bad = 0
    for path in args.files:
        try:
            left = relative_targets(description_of(path))
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            print(f"pypi_readme: {exc}", file=sys.stderr)
            return 2
        for target in left:
            print(f"{path}: relative link {target}")
        bad += len(left)
        if not left:
            print(f"{path}: every link and image is absolute")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
