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


def fake(bin_dir: Path, name: str, log: Path, then_exec: bool = False, exit_code: int = 0) -> None:
    body = f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{log}"\n'
    if then_exec:
        body += 'shift 2\nexec "$@"\n'  # drop its own two arguments, then run the command it wraps
    body += f"exit {exit_code}\n"
    (bin_dir / name).write_text(body, encoding="utf-8")
    (bin_dir / name).chmod(0o755)


def run_hook(tmp_path: Path, wrap: str | None, uv_exit: int = 0, expect_rc: int = 0) -> list[str]:
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
    fake(bin_dir, "uvx", log)
    fake(bin_dir, "uv", log, exit_code=uv_exit)
    fake(bin_dir, "queue-wrap", log, then_exec=True)
    done = subprocess.run(
        [shutil.which("bash"), str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == expect_rc, done.stderr
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


def test_a_failing_suite_fails_the_push_through_the_wrapper(tmp_path):
    calls = run_hook(tmp_path, "queue-wrap --wait 60m", uv_exit=3, expect_rc=3)
    assert calls[-1] == "uv run --locked pytest -q"


def test_a_spaces_only_wrap_is_the_same_as_unset(tmp_path):
    assert run_hook(tmp_path, "   ") == ["uvx prek run --all-files", "uv run --locked pytest -q"]


# --- which tests: the selector (scripts/affected_tests.py) and what the hook does with its answer ---------------------

SELECTOR = HOOK.parents[2] / "scripts" / "affected_tests.py"
SUITE = "uv run --locked pytest -q"
BASE_FILES = {
    "src/ws/__init__.py": "",
    "src/ws/a.py": "X = 1\n",
    "src/ws/b.py": "from ws import a\n",
    "src/ws/c.py": "Y = 1\n",
    "tests/test_a.py": "from ws import a\n",
    "tests/test_b.py": "from ws import b\n",
    "tests/test_c.py": "from ws import c\n",
    "tests/test_prepush_hook.py": "",
    "tests/test_cli.py": "",
    "tests/test_cli_registry.py": "",
    "README.md": "hi\n",
}


def run_selected(
    tmp_path: Path,
    change: dict[str, str | None],
    extra_env: dict[str, str] | None = None,
    selector: bool = True,
    fake_uv: bool = True,
) -> tuple[list[str], str]:
    """A scratch repo with modules a, b (imports a), c and a test for each, on a base commit that is `origin/main`.
    `change` is then committed on top: path -> new text, or None to delete. Returns the fake uv calls and stderr."""
    repo, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir(exist_ok=True)
    git = shutil.which("git")
    assert git, "git is needed"
    for rel, text in BASE_FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    if selector:
        (repo / "scripts").mkdir()
        shutil.copy(SELECTOR, repo / "scripts" / "affected_tests.py")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "WS_PREPUSH"))}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.path.dirname(git)}{os.pathsep}/usr/bin:/bin", HOME=str(tmp_path))
    env.update(extra_env or {})

    def g(*args: str) -> None:
        cfg = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run([git, "-C", str(repo), *cfg, *args], check=True, env=env, capture_output=True)

    g("init", "-q", "-b", "work")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    for rel, text in change.items():
        if text is None:
            (repo / rel).unlink()
        else:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "change")
    fake(bin_dir, "uvx", log)
    if fake_uv:
        fake(bin_dir, "uv", log)
    done = subprocess.run(
        [shutil.which("bash"), str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return [c for c in log.read_text(encoding="utf-8").splitlines() if c.startswith("uv ")], done.stderr


def test_a_one_module_change_runs_its_tests_only(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"})
    assert calls[-1] == f"{SUITE} tests/test_c.py"
    assert "1 test file(s)" in err


def test_a_module_change_runs_the_tests_of_what_imports_it(tmp_path):
    calls, _ = run_selected(tmp_path, {"src/ws/a.py": "X = 2\n"})
    assert calls[-1] == f"{SUITE} tests/test_a.py tests/test_b.py"


def test_a_new_test_file_is_always_selected(tmp_path):
    calls, _ = run_selected(tmp_path, {"tests/test_new.py": "def test_x():\n    pass\n"})
    assert calls[-1] == f"{SUITE} tests/test_new.py"


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "tests/sub/conftest.py",
        "tests/plugins/ws_harness.py",
        ".github/workflows/ci.yml",
        "scripts/affected_tests.py",
    ],
)
def test_a_force_full_path_runs_everything(tmp_path, path):
    calls, err = run_selected(tmp_path, {path: "x = 1\n"})
    assert calls[-1] == SUITE
    assert "the whole suite" in err


def test_a_deleted_file_runs_everything(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": None})
    assert calls[-1] == SUITE
    assert "renamed or deleted" in err


def test_nothing_to_select_runs_the_smoke_set_never_zero(tmp_path):
    calls, err = run_selected(tmp_path, {"README.md": "changed\n"})
    assert calls[-1] == f"{SUITE} tests/test_cli.py tests/test_cli_registry.py tests/test_prepush_hook.py"
    assert "SMOKE" in err


def test_a_selector_that_errors_runs_everything(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "def broken(:\n"})
    assert calls[-1] == SUITE
    assert "selector error" in err


def test_no_selector_in_the_checkout_runs_everything(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"}, selector=False)
    assert calls[-1] == SUITE
    assert "no selector" in err


def test_a_collection_error_in_the_selected_set_runs_everything(tmp_path):
    # the fake uv fails only the collect-only call, as pytest does for a test that will not import
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body = '#!/usr/bin/env bash\nprintf "%s\\n" "uv $*" >> "$LOG"\n[[ "$*" == *--collect-only* ]] && exit 2\nexit 0\n'
    (bin_dir / "uv").write_text(body, encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"}, {"LOG": str(tmp_path / "calls.log")}, fake_uv=False)
    assert calls[-1] == SUITE
    assert "did not collect" in err


def test_a_forced_full_run_is_logged_and_runs_everything(tmp_path):
    calls, _ = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"}, {"WS_PREPUSH_FULL": "1"})
    assert calls[-1] == SUITE
    assert "WS_PREPUSH_FULL=1" in (tmp_path / "repo" / ".git" / "ws-prepush-skips.log").read_text(encoding="utf-8")
