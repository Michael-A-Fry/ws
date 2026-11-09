"""The browser tests must run in CI, and must not read a home folder: both are checked from the files themselves."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
BROWSER_TESTS = ROOT / "tests" / "e2e" / "test_term_browser.py"


def job(name: str) -> str:
    """The text of one top-level job in ci.yml."""
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", CI, re.DOTALL | re.MULTILINE)
    assert m, f"no job named {name} in ci.yml"
    return m.group(1)


def test_ci_has_a_job_that_runs_the_browser_tests_and_fails_if_they_cannot_run():
    text = job("term-browser")
    assert re.search(r'WS_E2E_REQUIRE:\s*"1"', text), "a missing browser must fail there, not skip"
    assert "tests/e2e/test_*_browser.py" in text, "the job runs every browser test file, by glob"
    assert BROWSER_TESTS in set(ROOT.glob("tests/e2e/test_*_browser.py")), (
        "and the glob still finds the terminal's file"
    )


def test_that_job_installs_chromium_from_a_pinned_playwright():
    text = job("term-browser")
    pin = re.search(r'PLAYWRIGHT_VERSION:\s*"(\d+\.\d+\.\d+)"', text)
    assert pin, "Playwright is pinned to an exact version"
    assert "playwright==$PLAYWRIGHT_VERSION" in text
    assert "playwright install" in text and "chromium" in text


def test_every_action_in_ci_is_pinned_by_commit_sha():
    uses = re.findall(r"^\s+- uses: (\S+)", CI, re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_the_browser_tests_look_in_no_home_folder_for_a_browser():
    source = BROWSER_TESTS.read_text()
    for banned in ("Path.home()", "expanduser", 'os.environ["HOME"]', 'os.getenv("HOME")'):
        assert banned not in source, f"{banned} reads the real home"
    assert "PLAYWRIGHT_BROWSERS_PATH" in source
