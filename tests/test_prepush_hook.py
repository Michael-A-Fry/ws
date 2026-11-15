"""The pre-push hook: WS_PREPUSH_WRAP puts a command in front of the suite (a machine that queues test runs), and unset
the suite runs as it always did. The hook runs against a scratch repository with fake `uv`, `uvx` and wrapper commands
on PATH that only write down how they were called; nothing real runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".github" / "hooks" / "pre-push"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")


def fake(bin_dir: Path, name: str, log: Path, then_exec: bool = False) -> None:
    body = f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{log}"\n'
    if then_exec:
        body += 'shift 2\nexec "$@"\n'  # drop its own two arguments, then run the command it wraps'
    (bin_dir / name).write_text(body, encoding="utf-8")
    (bin_dir / name).chmod(0o755)


def run_hook(tmp_path: Path, wrap: str | None) -> list[str]:
    repo, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.log"
    repo.mkdir()
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git, "git is needed"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "WS_PREPUSH"))}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.path.dirname(git)}{os.pathsep}/usr/bin:/bin", HOME=str(tmp_path))
    if wrap is not None:
        env["WS_PREPUSH_WRAP"] = wrap
    subprocess.run([git, "init", "-q", str(repo)], check=True, env=env)
    for name in ("uv", "uvx"):
        fake(bin_dir, name, log)
    fake(bin_dir, "queue-wrap", log, then_exec=True)
    done = subprocess.run(
        [shutil.which("bash"), str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return log.read_text(encoding="utf-8").splitlines()


def test_unset_the_suite_runs_as_it_always_did(tmp_path):
    assert run_hook(tmp_path, None) == ["uvx prek run --all-files", "uv run --locked pytest -q"]


def test_an_empty_wrap_is_the_same_as_unset(tmp_path):
    assert run_hook(tmp_path, "") == ["uvx prek run --all-files", "uv run --locked pytest -q"]


def test_the_wrapper_runs_the_suite_and_only_the_suite(tmp_path):
    calls = run_hook(tmp_path, "queue-wrap --wait 60m")
    assert calls == [
        "uvx prek run --all-files",
        "queue-wrap --wait 60m uv run --locked pytest -q",
        "uv run --locked pytest -q",
    ]
