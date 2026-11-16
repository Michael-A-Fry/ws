#!/usr/bin/env python3
"""Pick the test files a change can affect, so a push runs those and CI runs the whole suite.

The default is a static import map: each test file is mapped to the `ws` modules it imports, followed transitively
through `src/ws`. What imports cannot see is added by a table in this file (FORCE_FULL, CLI_ROOTS) and by a word
search: a changed non-Python file or a changed module name selects every test whose source mentions it. Anything the
map cannot judge selects everything (exit 0, first line `FULL: <reason>`), never nothing.

Output, one thing per line on stdout: `FULL: <reason>`, or `SELECT` followed by test paths, or `SMOKE` when the change
touches no test (docs only). Exit 0 whenever it answered; 2 on a usage error. The hook treats any other exit, an
empty answer, or a crash as "run everything".

    python3 scripts/affected_tests.py --base origin/main
    python3 scripts/affected_tests.py --files src/ws/gate.py docs/x.md
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ws"
TESTS = ROOT / "tests"

# A change to any of these can reach every test through the harness, the lock or the CI wiring: run the lot.
FORCE_FULL_PREFIXES = ("pyproject.toml", "uv.lock", "tests/plugins/", ".github/", "scripts/affected_tests.py")
FORCE_FULL_NAMES = ("conftest.py",)

# A test that starts `python -m ws` or calls the CLI reaches commands through cli.py's scan, which no import shows.
# Such a test is selected by a changed module whose name its text mentions (a command name is its module name).
CLI_MARKERS = re.compile(r'"-m",\s*"ws"|-m ws\b|\bws\.cli\b|\bcli\.main\b|run_ws\(|\bws_cli\b')

# Docs-only and similar changes that no test reads still run a short named set rather than nothing.
SMOKE = ("tests/test_cli.py", "tests/test_cli_registry.py", "tests/test_prepush_hook.py")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def changed(base: str) -> tuple[list[str], list[str], list[str]]:
    """(modified or added paths, added paths, renamed or deleted paths) of HEAD against the merge-base with `base`."""
    mb = git("merge-base", base, "HEAD").strip()
    mod, added, gone = [], [], []
    for line in git("diff", "--name-status", "-M", f"{mb}..HEAD").splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status in "RD":
            gone.append(parts[1])
            if status == "R":
                mod.append(parts[2])
        else:
            mod.append(parts[1])
            if status == "A":
                added.append(parts[1])
    return mod, added, gone


def module_of(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def src_modules() -> dict[str, Path]:
    return {module_of(p): p for p in SRC.rglob("*.py")}


def imports_of(tree: ast.AST, here: str, is_pkg: bool) -> set[str]:
    """Every `ws` module a parsed file imports, lazy imports inside functions included."""
    mods = src_modules()
    out: set[str] = set()

    def add(name: str) -> None:
        while name:
            if name in mods:
                out.add(name)
            name = name.rpartition(".")[0]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = here.split(".")
                pkg = pkg if is_pkg else pkg[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([base] if base else []))
            add(base)
            for a in node.names:
                add(f"{base}.{a.name}")
    return out


@cache
def deps(mod: str) -> frozenset[str]:
    """Direct ws imports of one src module."""
    path = src_modules()[mod]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(imports_of(tree, mod, path.name == "__init__.py")) - {mod}


def closure(roots: set[str]) -> set[str]:
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        todo.extend(deps(m))
        parent = m.rpartition(".")[0]
        if parent in src_modules():  # importing ws.a.b runs ws/a/__init__.py
            todo.append(parent)
    return seen


@cache
def command_modules() -> frozenset[str]:
    return frozenset(
        m for m, p in src_modules().items() if re.search(r'^COMMAND = "', p.read_text(encoding="utf-8"), re.MULTILINE)
    )


def test_files() -> list[Path]:
    return sorted(TESTS.rglob("test_*.py"))


def test_info(path: Path) -> tuple[str, set[str]]:
    """The test's source text and the ws modules its imports reach (its own, its siblings' helper imports, conftest)."""
    text = path.read_text(encoding="utf-8")
    here = path.stem
    roots = imports_of(ast.parse(text), here, False)
    # a local helper (tests/helpers/x.py, tests/_x.py) the test imports brings its own imports
    for helper in list(TESTS.rglob("*.py")):
        if (
            helper.stem != here
            and not helper.name.startswith("test_")
            and re.search(rf"\b{re.escape(helper.stem)}\b", text)
        ):
            roots |= imports_of(ast.parse(helper.read_text(encoding="utf-8")), helper.stem, False)
    reach = closure(roots)
    return text, reach


def words_for(path: str) -> list[str]:
    """What a test's text would say if it read this file: the path, its basename, its parent folder."""
    p = Path(path)
    out = [path, p.name]
    if len(p.parts) > 1 and p.parent.as_posix() not in {"src/ws", "tests", "."}:
        out.append(p.parent.as_posix() + "/")
    return out


def select(mod: list[str], added: list[str], gone: list[str]) -> tuple[str, list[str]]:
    if gone:
        return "FULL: a file was renamed or deleted: " + gone[0], []
    allp = mod + added
    for p in allp:
        if p.startswith(FORCE_FULL_PREFIXES) or Path(p).name in FORCE_FULL_NAMES:
            return f"FULL: {p} can reach every test", []
    tests = [t for t in test_files()]
    rel = {t: t.relative_to(ROOT).as_posix() for t in tests}
    chosen: set[str] = set()
    info = {t: test_info(t) for t in tests}
    changed_mods: set[str] = set()
    other: list[str] = []
    for p in allp:
        if p in rel.values():
            chosen.add(p)
        elif p.startswith("src/ws/") and p.endswith(".py"):
            changed_mods.add(module_of(ROOT / p))
        elif p.startswith("tests/") and p.endswith(".py"):
            # a helper, not a test: every test that names it
            other.append(p)
        else:
            other.append(p)
    for t, (text, reach) in info.items():
        if changed_mods & reach:
            chosen.add(rel[t])
        for m in changed_mods:  # what imports cannot see (a CLI test, a scanned folder): tests that name the module
            by_word = CLI_MARKERS.search(text) or m in command_modules() or m.count(".") > 1
            if by_word and re.search(rf"\b{re.escape(m.rpartition('.')[2])}\b", text):
                chosen.add(rel[t])
        for p in other:
            if any(w in text for w in words_for(p)):
                chosen.add(rel[t])
    if not chosen:
        return "SMOKE", [s for s in SMOKE if (ROOT / s).exists()]
    return "SELECT", sorted(chosen)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--files", nargs="*", help="treat these paths as the (modified) change instead of git")
    a = ap.parse_args(argv)
    try:
        mod, added, gone = (a.files, [], []) if a.files is not None else changed(a.base)
        kind, tests = select(mod, added, gone)
    except Exception as e:  # noqa: BLE001  the hook treats any failure as "run everything"; say why
        print(f"FULL: selector error: {type(e).__name__}: {e}")
        return 0
    print(kind)
    for t in tests:
        print(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
