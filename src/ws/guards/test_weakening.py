"""test-weakening: a session that is told to make the tests pass doesn't make them pass by weakening them. Before a
write to a test file, the edit is applied to the file on disk to get its text before and after, and the two are compared.

A test file is one under `tests/`, `test/` or `__tests__/`, or named `test_*.py`, `*_test.*`, `*.test.js`, `*.test.jsx`,
`*.test.ts`, `*.test.tsx` or `test-*.R`; never one under `fixtures/`, `testdata/`, `snapshots/` or `golden/`. The
table's `paths` replaces the first list and `except` the second (globs: `**` crosses folders; a pattern with no `/`
matches at any depth).

Deny: a skip or expected-failure marker added to a test that already existed (a decorator, a skip call, `it.skip`,
`xit`, `xdescribe`, `t.Skip`, `skip_if(...)`, `#[ignore]`, `@Disabled`), counted for each test, so a new test or a new
file may carry one; or a marker added outside any test (a class decorator, `pytestmark`, `describe.skip`) in a file that
had tests. A plain `describe(` block is not a marker. The deny is approvable: the person can allow it once with
`ws approve`.
Warn: a test removed, fewer assertion lines in the file, or an always-true assertion added (`assert True`,
`assert x == x`, `expect(true).toBe(true)`, `expect_true(TRUE)`).

A test is found by its line, not parsed: `def test...` (with its decorators), `it(` and `test(` with a title,
`test_that("...")`, `func TestX(`, `fn test_x(`, and a Java or Kotlin method after `@Test`. It runs to the next test
or, in Python, to the next line at its own indent or less. A write whose text can't be got (a file that doesn't exist
yet, an edit whose old text isn't in the file, a notebook) is not judged.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ws.approvals import ApprovalError
from ws.approvals import _read_regular as read_regular
from ws.guards import Finding, known_keys, text_list
from ws.guards._paths import matches, relative

NAME = "test-weakening"
EVENTS = ("pre",)
KINDS = ("write",)

DEFAULT_PATHS = (
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/test_*.py",
    "**/*_test.*",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/test-*.R",
)
DEFAULT_EXCEPT = ("**/fixtures/**", "**/testdata/**", "**/snapshots/**", "**/golden/**")
MAX_BYTES = 2 << 20


@dataclass(frozen=True)
class Config:
    paths: tuple[str, ...] = DEFAULT_PATHS
    exclude: tuple[str, ...] = DEFAULT_EXCEPT


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("paths", "except"))
    return Config(text_list(table, "paths", where, DEFAULT_PATHS), text_list(table, "except", where, DEFAULT_EXCEPT))


# --- reading a test file -------------------------------------------------------------------------------------------

_SKIP = re.compile(
    r"""@?\bpytest\.mark\.(?:skip|skipif|xfail)\b | \bpytest\.(?:skip|xfail)\s*\( | @unittest\.(?:skip\w*|expectedFailure)\b
    | \.skipTest\s*\( | @(?:skip|skipif|xfail|expectedFailure)\b
    | \b(?:it|test|describe)\.(?:skip|fixme)\b | \b(?:xit|xtest|xdescribe)\s*\(
    | (?<![\w.])skip(?:_if|_if_not|_on_cran|_on_os|_on_ci|_on_bioc)?\s*\(
    | \bt\.Skip(?:f|Now)?\s*\( | \#\[ignore | @Ignore\b | @Disabled\b""",
    re.VERBOSE,
)
_ASSERT = re.compile(
    r"""^\s*assert\b | \bself\.assert\w*\( | \bassert_\w+\( | \bassert\w*\.\w+\( | \bpytest\.raises\( | \bexpect\w*\(
    | \bt\.(?:Error|Errorf|Fatal|Fatalf)\( | \b(?:ASSERT|EXPECT)_\w+\( | \bassert!?\( | \bassert_eq!
    | \.should\b | \brequire\.\w+\(""",
    re.VERBOSE,
)
_TRUE = re.compile(
    r"""^\s*assert\s+(?:True|1|not\s+False)\s*(?:[,#].*)?$ | ^\s*assert\s+(\w[\w.]*)\s*==\s*\1\s*(?:[,#].*)?$
    | \bassert(?:True|That)\(\s*True\s*\) | \bassertEquals?\(\s*(\w+)\s*,\s*\2\s*\)
    | \bexpect\(\s*true\s*\)\s*\.\s*(?:toBe|toEqual|toBeTruthy)\b | \bexpect\(\s*(\w+)\s*\)\s*\.\s*toBe\(\s*\3\s*\)
    | \bexpect_true\(\s*TRUE\s*\) | \bassert\s*\(\s*true\s*\)""",
    re.VERBOSE,
)
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
_OTHER = (
    re.compile(r"""\b(?:it|test)(?:\.\w+)?\s*\(\s*(['"`])(.+?)\1"""),  # JavaScript and TypeScript
    re.compile(r"""\b(?:xit|xtest)\s*\(\s*(['"`])(.+?)\1"""),
    re.compile(r"""\btest_that\s*\(\s*(['"`])(.+?)\1"""),  # R
    re.compile(r"\bfunc\s+()(Test\w*)\s*\("),  # Go
    re.compile(r"\bfn\s+()(test\w*)\s*\("),  # Rust
    re.compile(r"\bvoid\s+()(\w+)\s*\("),  # a Java method (only after an @Test line, below)
)


def _code(line: str) -> str:
    """The line with a whole-line comment blanked (`#[` is an attribute, not a comment)."""
    s = line.lstrip()
    return "" if (s.startswith("#") and not s.startswith("#[")) or s.startswith(("//", "*", "/*")) else line


def _lines(text: str) -> list[str]:
    return [_code(x) for x in text.splitlines()]


def _blocks(code: list[str], is_python: bool) -> tuple[dict[str, list[str]], list[str]]:
    """(each test's lines by title, the lines outside every test). Titles that repeat are joined."""
    spans: list[tuple[int, int, str]] = []
    i, after_test_attr = 0, False
    while i < len(code):
        line = code[i]
        py = _PY_DEF.match(line)
        title = None
        if py:
            title = py.group(2)
        elif not is_python:
            for n, rx in enumerate(_OTHER):
                if n == len(_OTHER) - 1 and not after_test_attr:
                    continue
                if m := rx.search(line):
                    title = m.group(2)
                    break
        after_test_attr = "@Test" in line or (after_test_attr and not line.strip())
        if title is None:
            i += 1
            continue
        start = i
        if py:
            while start > 0 and code[start - 1].lstrip().startswith("@"):
                start -= 1  # the decorators belong to the test below them
            indent = len(py.group(1))
            end = i + 1
            while end < len(code) and (not code[end].strip() or len(code[end]) - len(code[end].lstrip()) > indent):
                end += 1
        else:
            end = i + 1
            while end < len(code) and not any(rx.search(code[end]) for rx in _OTHER[:5]):
                end += 1
        spans.append((start, end, title))
        i = max(end, i + 1)
    inside = {n for s, e, _ in spans for n in range(s, e)}
    tests: dict[str, list[str]] = {}
    for s, e, title in spans:
        tests.setdefault(title, []).extend(code[s:e])
    return tests, [x for n, x in enumerate(code) if n not in inside]


def _count(rx: re.Pattern, lines) -> int:
    return sum(1 for x in lines if rx.search(x))


@dataclass(frozen=True)
class Reading:
    skips: dict[str, int]  # for each test title
    loose_skips: int  # skip markers outside every test
    asserts: int
    always_true: Counter
    tests: frozenset[str]


def _read(text: str, is_python: bool) -> Reading:
    code = _lines(text)
    tests, loose = _blocks(code, is_python)
    return Reading(
        {t: _count(_SKIP, ls) for t, ls in tests.items()},
        _count(_SKIP, loose),
        _count(_ASSERT, code),
        Counter(x.strip() for x in code if _TRUE.search(x)),
        frozenset(tests),
    )


def compare(before: str, after: str, is_python: bool) -> tuple[list[str], list[str]]:
    """(reasons to deny, reasons to warn) for an edit from `before` to `after`."""
    a, b = _read(before, is_python), _read(after, is_python)
    deny = [
        f"a skip or expected-failure marker was added to the existing test {t}"
        for t in sorted(a.tests)
        if t in b.tests and b.skips[t] > a.skips[t]
    ]
    if b.loose_skips > a.loose_skips and a.tests:
        deny.append("a skip or expected-failure marker was added to a class or file that already has tests")
    warn = []
    if gone := sorted(a.tests - b.tests):
        warn.append(f"test removed: {', '.join(gone[:5])}" + (f" and {len(gone) - 5} more" if len(gone) > 5 else ""))
    if b.asserts < a.asserts:
        warn.append(f"fewer assertion lines ({a.asserts} before, {b.asserts} after)")
    if added := sorted((b.always_true - a.always_true).elements()):
        warn.append(f"an assertion that is always true was added: {added[0][:60]}")
    return deny, warn


# --- applying the edit ---------------------------------------------------------------------------------------------


def _apply(text: str, call) -> str | None:
    """The file's text after the write, or None when the call doesn't give enough to know."""
    d = call.data
    if d.get("tool") == "NotebookEdit":
        return None
    if isinstance(d.get("content"), str):
        return d["content"]
    edits = d.get("edits") if d.get("edits") is not None else [{"old": d.get("old"), "new": d.get("new")}]
    for e in edits:
        old, new = e.get("old"), e.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or old not in text:
            return None
        text = text.replace(old, new)  # more than one match is an edit that replaces them all
    return text


def judge(call, cfg: Config, ctx) -> Finding | None:
    rel = relative(call.value, ctx.cwd)
    if not matches(cfg.paths, rel) or matches(cfg.exclude, rel):
        return None
    path = Path(call.value) if call.value.startswith("/") else Path(ctx.cwd) / call.value
    try:
        before = read_regular(path, MAX_BYTES)
    except ApprovalError:
        return None  # a new file, or one it can't read: nothing to compare with
    after = _apply(before, call)
    if after is None:
        return None
    deny, warn = compare(before, after, path.suffix == ".py")
    if deny:
        why = f"{rel}: " + "; ".join(deny)
        return Finding(
            "deny",
            f"{why}. Make the test pass. If the person agrees to the change, they can allow it once with `ws approve`",
            approvable=True,
        )
    if warn:
        return Finding("warn", f"{rel}: " + "; ".join(warn))
    return None
