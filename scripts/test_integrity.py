"""T1, the test-integrity check: a pull request that weakens the tests, or the machinery that runs them, says so.

It compares the tests in `BASE` with the tests in `HEAD` and lists what makes a test weaker: an assertion removed (or a
whole test file deleted), a tolerance widened, added or changed to another kind (`approx`, `abs=`, `rel=`, `atol=`,
`rtol=`), a skip or expected-failure marker added to a file that already existed, and an edit to the test machinery
(any conftest.py, tests/plugins/, the pytest tables in pyproject.toml, .github/workflows/, .pre-commit-config.yaml,
this script, and the code that defines what a weakened test is). Any of those needs a non-empty `test-change:` line in
the pull request body saying why. An edit to anything under tests/holdout/ needs a non-empty `holdout-change:` line.

What counts as a removed assertion, a removed test or a new skip is `ws.guards.test_weakening`, imported: the guard that
warns a session while it edits a test and this check agree. The extras (tolerances, configuration, the holdout) live here.

    PR_BODY="$BODY" python3 scripts/test_integrity.py --base "$BASE_SHA"

The body comes only through the environment (`--body-env` names the variable), never the command line, and comments in it
(`<!-- ... -->`, such as the template's) are dropped before the lines are looked for. An empty or missing body is no lines.
A base commit that isn't in the clone is an error (exit 2), not a pass. Exit 0: nothing to say, or every hit has its
line. Exit 1: a hit with no line. Exit 2: the check couldn't run. Stdlib only, plus ws itself.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ws.guards import test_weakening as tw
from ws.guards._paths import matches

TEST_CHANGE = "test-change"
HOLDOUT_CHANGE = "holdout-change"
HOLDOUT = "tests/holdout/"
MACHINERY = (
    "conftest.py",
    "tests/**/conftest.py",
    "src/**/conftest.py",
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "tests/plugins/**",
    "tests/gate_corpus/**",  # rows of expected decisions: no assertion to count, so any edit is declared
    ".github/workflows/**",
    ".pre-commit-config.yaml",
    "scripts/test_integrity.py",
    "src/ws/guards/test_weakening.py",  # what counts as a weakened test is defined here and in _paths
    "src/ws/guards/_paths.py",
    "src/ws/__init__.py",  # the rest is what this script imports
    "src/ws/guards/__init__.py",
    "src/ws/approvals.py",
)
PYPROJECT = "pyproject.toml"

# A tolerance written as a keyword (`abs=1e-6`, `atol=0.01`, `rel=.5`) or as approx's second argument.
_NUM = r"(?:[0-9][0-9_.]*|\.[0-9][0-9_]*)(?:[eE][-+]?\d+)?"
_TOL_KEY = re.compile(rf"\b(rel|abs|rel_tol|abs_tol|atol|rtol|tol|tolerance|delta)\s*=\s*({_NUM})")
_TOL_POS = re.compile(rf"\bapprox\(\s*[^,()]+,\s*({_NUM})\s*[,)]")
_APPROX = re.compile(r"\bapprox\(")


class IntegrityError(Exception):
    """The check couldn't run (not a verdict on the pull request). `kind` says why."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Hit:
    path: str
    why: str
    needs: str  # TEST_CHANGE or HOLDOUT_CHANGE

    def __str__(self) -> str:
        # a file name is the pull request's text: one with a line break could write its own line in the job's log
        return f"{self.path.encode('unicode_escape').decode()}: {self.why}"


# --- git ---------------------------------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if done.returncode != 0:
        raise IntegrityError("git", f"git {args[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def resolve(repo: Path, rev: str, what: str) -> str:
    """The commit `rev` names, or the typed error a shallow clone gives for a base that isn't in it."""
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise IntegrityError(
            "missing-commit",
            f"the {what} commit {rev!r} isn't in this clone (a shallow clone? fetch the full history, fetch-depth: 0)",
        )
    return done.stdout.decode().strip()


def changed(repo: Path, start: str, head: str) -> list[tuple[str, str, str]]:
    """(status letter, old path, new path) for what `head` changed since `start`, renames followed."""
    parts = (
        git(repo, "diff", "--name-status", "-z", "-M", "--no-ext-diff", start, head)
        .decode("utf-8", "surrogateescape")  # a name that isn't UTF-8 goes back to git as the same bytes
        .split("\0")
    )
    out, i = [], 0
    while i < len(parts) and parts[i]:
        status = parts[i][0]
        if status in "RC":
            out.append((status, parts[i + 1], parts[i + 2]))
            i += 3
        else:
            out.append((status, parts[i + 1], parts[i + 1]))
            i += 2
    return out


def blob(repo: Path, rev: str, path: str, must: bool = True) -> str:
    """The file's text at `rev`. A file that should be there and can't be read is a typed error, never an empty file that
    compares equal to anything; with `must` False, "" when there is none. UnicodeDecodeError for a file that isn't text."""
    done = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{rev}:{path}"], capture_output=True, check=False
    )
    if done.returncode == 0:
        return done.stdout.decode("utf-8")
    if must:
        raise IntegrityError("git", f"can't read {path!r} at {rev}")
    return ""


# --- reading the changes -----------------------------------------------------------------------------------------------


def is_test_file(rel: str) -> bool:
    return matches(tw.DEFAULT_PATHS, rel) and not matches(tw.DEFAULT_EXCEPT, rel)


def pytest_tables(text: str) -> str:
    """The `[tool.pytest...]` tables of a pyproject.toml, as text, to compare before with after."""
    keep, on = [], False
    for line in text.splitlines():
        if line.startswith("["):
            on = line.startswith("[tool.pytest")
        if on and line.strip() and not line.lstrip().startswith("#"):
            keep.append(line.rstrip())
    return "\n".join(keep)


def tolerances(line: str) -> list[tuple[str, float]]:
    """(kind, value) for each tolerance on the line; a bare `approx(x)` is the default rel of 1e-6."""
    found = [(m.group(1), float(m.group(2).replace("_", ""))) for m in _TOL_KEY.finditer(line)]
    found += [("rel", float(m.group(1).replace("_", ""))) for m in _TOL_POS.finditer(line)]
    if not found and _APPROX.search(line):
        found.append(("rel", 1e-6))
    return found


def widened(before: str, after: str) -> list[str]:
    """The lines of `before` that `after` changes to a looser tolerance, or from an exact comparison to an approximate one."""
    a, b = before.splitlines(), after.splitlines()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        old = [t for x in a[i1:i2] for t in tolerances(x)]
        new = [t for x in b[j1:j2] for t in tolerances(x)]
        if not new:
            continue
        loosest = {k: max(v for kk, v in old if kk == k) for k, _ in old}
        # a bigger value, or a kind the old line did not have (`approx(x)` to `approx(x, abs=0.5)`, `abs=` to `rel=`)
        looser = any(k not in loosest or v > loosest[k] for k, v in new)
        introduced = not old and any(_APPROX.search(x) or _TOL_KEY.search(x) for x in b[j1:j2])
        if looser or introduced:
            out.append(b[j1].strip()[:80])
    return out


def skips(reading) -> int:
    return sum(reading.skips.values()) + reading.loose_skips


def judge_test(path: str, before: str, after: str) -> list[str]:
    """Why an edit from `before` to `after` makes the tests weaker (empty when it doesn't)."""
    is_python = path.endswith(".py")
    deny, warn = tw.compare(before, after, is_python)
    why = [*deny, *warn]
    if before and skips(tw._read(after, is_python)) > skips(tw._read(before, is_python)):
        why.append("a skip or expected-failure marker was added to a file that already existed")
    if before and (loose := widened(before, after)):
        why.append(f"a tolerance was widened or added: {loose[0]}")
    return list(dict.fromkeys(why))


def check(repo: Path, base: str, head: str = "HEAD") -> list[Hit]:
    """Every hit the change from `base` to `head` makes, in path order."""
    base_c, head_c = resolve(repo, base, "base"), resolve(repo, head, "head")
    start = git(repo, "merge-base", base_c, head_c).decode().strip()  # a base that moved on isn't part of the change
    hits: list[Hit] = []
    for status, old, new in sorted(changed(repo, start, head_c), key=lambda x: x[2]):
        for path in dict.fromkeys((old, new)):
            if path.startswith(HOLDOUT):
                hits.append(Hit(path, "an edit to the holdout set", HOLDOUT_CHANGE))
        if matches(MACHINERY, new) or matches(MACHINERY, old):
            hits.append(Hit(new, "an edit to the test machinery or CI", TEST_CHANGE))
        elif PYPROJECT in (old, new) and pytest_tables(blob(repo, start, old, must=False)) != pytest_tables(
            blob(repo, head_c, new, must=False)
        ):
            hits.append(Hit(new, "an edit to the pytest configuration", TEST_CHANGE))
        if not (is_test_file(old) or is_test_file(new)) or status == "A":
            continue
        try:
            before = blob(repo, start, old)
            after = "" if status == "D" or not is_test_file(new) else blob(repo, head_c, new)
        except UnicodeDecodeError:
            hits.append(Hit(new, "a test file that isn't text, so it was not compared", TEST_CHANGE))
            continue
        whys = judge_test(old, before, after)
        if status == "D" and not whys and old.endswith(".py"):
            whys = ["a test file with no assertion lines to count"]
        for why in whys:
            hits.append(
                Hit(
                    old if status == "D" else new,
                    why + (" (the file was deleted)" if status == "D" else ""),
                    TEST_CHANGE,
                )
            )
    return hits


# --- the pull request body ---------------------------------------------------------------------------------------------


def has_line(body: str, key: str) -> bool:
    """True when a line of the body (comments dropped) starts `key:` and has something after it."""
    text = re.sub(r"<!--.*?-->", "", body.replace("\r", ""), flags=re.DOTALL)
    return any(
        m.group(1).strip() for m in re.finditer(rf"^[ \t]*{re.escape(key)}:(.*)$", text, re.MULTILINE | re.IGNORECASE)
    )


def verdict(hits: list[Hit], body: str) -> tuple[int, list[str]]:
    """(exit code, lines to print)."""
    if not hits:
        return 0, ["test-integrity: no test was weakened, no machinery changed, the holdout is untouched"]
    lines = [f"  {h}" for h in hits]
    missing = sorted({h.needs for h in hits if not has_line(body, h.needs)})
    if not missing:
        return 0, ["test-integrity: these changes are declared in the pull request body:", *lines]
    for key in missing:
        lines.append(f"test-integrity: add a non-empty `{key}:` line to the pull request body saying why")
    return 1, ["test-integrity: the tests or their machinery changed:", *lines]


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", required=True, help="the commit the pull request is based on (the event's base sha)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--body-env", default="PR_BODY", help="the environment variable that holds the pull request body")
    args = ap.parse_args(argv)
    body = (os.environ if environ is None else environ).get(args.body_env, "")
    try:
        code, lines = verdict(check(args.repo, args.base, args.head), body)
    except IntegrityError as exc:
        print(f"test-integrity: could not run ({exc.kind}): {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
