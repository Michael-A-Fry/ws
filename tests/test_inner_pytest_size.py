"""A test that starts a whole pytest run in a subprocess must be sized medium or large (the 60 s of a small test is not
enough for it on a loaded machine; pre-push went red on that twice)."""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

TESTS = Path(__file__).parent
BIG = {"medium", "large"}


def _marker_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
            names.add(node.attr)
    return names


def _starts_pytest(fn: ast.AST) -> bool:
    """True when the body holds a list that runs `-m pytest`: two neighbouring string items."""
    for node in ast.walk(fn):
        if isinstance(node, ast.List):
            strings = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None for e in node.elts]
            if any(a == "-m" and b == "pytest" for a, b in itertools.pairwise(strings)):
                return True
    return False


def unsized(source: str) -> list[str]:
    """The test functions in `source` that run `-m pytest` without a medium or large marker of their own."""
    tree = ast.parse(source)
    return [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        and fn.name.startswith("test_")
        and _starts_pytest(fn)
        and not _marker_names(fn) & BIG
    ]


def test_every_test_that_starts_an_inner_pytest_is_medium_or_large():
    bad = []
    for path in sorted(TESTS.rglob("test_*.py")):
        # a file-wide `pytestmark = pytest.mark.medium` counts for every test in it
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        if any(
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "pytestmark" for t in n.targets)
            and any(isinstance(c, ast.Attribute) and c.attr in BIG for c in ast.walk(n.value))
            for n in tree.body
        ):
            continue
        bad += [f"{path.relative_to(TESTS)}::{name}" for name in unsized(text)]
    assert bad == [], f"these start a pytest run and need @pytest.mark.medium (or large): {bad}"


def test_the_scan_catches_a_planted_unsized_test_and_accepts_a_sized_one():
    planted = "def test_x():\n    run([sys.executable, '-m', 'pytest', '-q'])\n"
    assert unsized(planted) == ["test_x"]
    sized = (
        "import pytest\n\n"
        + "@"
        + "pytest.mark.medium\ndef test_x():\n    run([sys.executable, '-m', 'pytest', '-q'])\n"
    )
    assert unsized(sized) == []
    assert unsized("def test_y():\n    run(['git', 'status'])\n") == []
