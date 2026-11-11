"""A suite run inside a git hook leaves the outer repository alone.

git sets GIT_DIR and its kin for a hook, and a pre-push hook runs this suite. tests/conftest.py clears them for the whole
run, so a test's `git -C <scratch repo> config` writes to the scratch repo and nowhere else.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SUITE_CONFTEST = Path(__file__).with_name("conftest.py")


def _suite_vars() -> tuple[str, ...]:
    """The list from the suite's own conftest, by path: `import conftest` can find a folder's conftest first."""
    spec = importlib.util.spec_from_file_location("ws_suite_conftest_vars", SUITE_CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._GIT_REPO_VARS


_GIT_REPO_VARS = _suite_vars()

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@needs_git
def test_the_list_is_what_git_names():
    named = set(git("rev-parse", "--local-env-vars").split())
    assert named <= set(_GIT_REPO_VARS), f"git names more variables than conftest clears: {named - set(_GIT_REPO_VARS)}"


def test_no_variable_of_an_outer_repository_reaches_a_test():
    assert not [name for name in _GIT_REPO_VARS if name in os.environ]


@needs_git
def test_a_run_with_an_outer_git_dir_writes_only_to_its_own_scratch_repo(tmp_path):
    outer = tmp_path / "outer"
    git("init", "-q", "--bare", str(outer))
    before = (outer / "config").read_text()
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_probe.py").write_text(
        "import subprocess\n"
        "def test_probe(tmp_path):\n"
        "    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)\n"
        "    subprocess.run(['git', '-C', str(tmp_path), 'config', 'probe.planted', 'yes'], check=True)\n"
    )
    (probe / "conftest.py").write_text(
        "import importlib.util\n"
        f"SUITE = {str(SUITE_CONFTEST)!r}\n"
        "spec = importlib.util.spec_from_file_location('ws_suite_conftest', SUITE)\n"
        "spec.loader.exec_module(importlib.util.module_from_spec(spec))  # the suite's own clearing runs on import\n"
    )
    env = dict(os.environ, GIT_DIR=str(outer), GIT_INDEX_FILE=str(outer / "index"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(probe)],
        env=env,
        capture_output=True,
        text=True,
        cwd=probe,
        check=False,  # the return code is asserted below, with the output
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (outer / "config").read_text() == before, "the outer repository's config was written"
