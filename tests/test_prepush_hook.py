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

SMOKE_ARGS = "uv run --locked pytest -q tests/test_cli.py tests/test_cli_registry.py tests/test_prepush_hook.py"
SMOKE_RUN = SMOKE_ARGS  # a run of the smoke set alone
CAPPED_SMOKE = SMOKE_ARGS + " -x"  # a capped run stops at the first failure
TIMEOUT = "timeout 300 " if shutil.which("timeout") else ""

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")


def fake(bin_dir: Path, name: str, log: Path, then_exec: bool = False, exit_code: int = 0, drop: int = 2) -> None:
    body = f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{log}"\n'
    if then_exec:
        body += f'shift {drop}\nexec "$@"\n'  # drop its own arguments, then run the command it wraps
    body += f"exit {exit_code}\n"
    (bin_dir / name).write_text(body, encoding="utf-8")
    (bin_dir / name).chmod(0o755)


def run_hook(
    tmp_path: Path,
    wrap: str | None,
    uv_exit: int = 0,
    expect_rc: int = 0,
    with_ws_test: bool = False,
    full: bool = False,
) -> list[str]:
    repo, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.log"
    repo.mkdir()
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git, "git is needed"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "WS_PREPUSH"))}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.path.dirname(git)}{os.pathsep}/usr/bin:/bin", HOME=str(tmp_path))
    if wrap is not None:
        env["WS_PREPUSH_WRAP"] = wrap
    if full:
        env["WS_PREPUSH_FULL"] = "1"
    subprocess.run([git, "init", "-q", str(repo)], check=True, env=env)
    fake(bin_dir, "uvx", log)
    fake(bin_dir, "uv", log, exit_code=uv_exit)
    fake(bin_dir, "queue-wrap", log, then_exec=True)
    if with_ws_test:
        fake(bin_dir, "ws-test", log, then_exec=True, drop=4)  # --light --node local --
    done = subprocess.run(
        [shutil.which("bash"), str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == expect_rc, done.stderr
    return log.read_text(encoding="utf-8").splitlines()


def test_unset_wrap_runs_the_tests_directly(tmp_path):
    assert run_hook(tmp_path, None) == ["uvx prek run --all-files", CAPPED_SMOKE]


def test_an_empty_wrap_is_the_same_as_unset(tmp_path):
    assert run_hook(tmp_path, "") == ["uvx prek run --all-files", CAPPED_SMOKE]


def test_the_wrapper_runs_the_tests_and_only_the_tests(tmp_path):
    calls = run_hook(tmp_path, "queue-wrap --wait 60m")
    assert calls == [
        "uvx prek run --all-files",
        "queue-wrap --wait 60m " + TIMEOUT + CAPPED_SMOKE,
        CAPPED_SMOKE,
    ]


def test_failing_tests_fail_the_push_through_the_wrapper(tmp_path):
    calls = run_hook(tmp_path, "queue-wrap --wait 60m", uv_exit=3, expect_rc=3)
    assert calls[-1] == CAPPED_SMOKE


def test_ws_test_on_the_path_is_the_default_wrapper(tmp_path):
    calls = run_hook(tmp_path, None, with_ws_test=True)
    assert calls == [
        "uvx prek run --all-files",
        "ws-test --light --node local -- " + TIMEOUT + CAPPED_SMOKE,
        CAPPED_SMOKE,
    ]


def test_a_whole_suite_run_uses_the_full_class_and_spaces_only_counts_as_unset(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    full = run_hook(tmp_path / "a", None, with_ws_test=True, full=True)
    assert full[1] == "ws-test --full --node local -- " + SUITE
    spaces = run_hook(tmp_path / "b", "   ", with_ws_test=True)
    assert spaces[1].startswith("ws-test --light --node local -- ")


def test_an_explicit_wrap_beats_ws_test_and_an_empty_one_turns_it_off(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    calls = run_hook(tmp_path / "a", "queue-wrap --wait 60m", with_ws_test=True)
    assert calls[1].startswith("queue-wrap --wait 60m ")
    assert run_hook(tmp_path / "b", "", with_ws_test=True) == ["uvx prek run --all-files", CAPPED_SMOKE]


def test_a_spaces_only_wrap_is_the_same_as_unset(tmp_path):
    assert run_hook(tmp_path, "   ") == ["uvx prek run --all-files", CAPPED_SMOKE]


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
    "src/ws/d.py": "Z = 1\n",
    "src/ws/e.py": "from ws import d\n",
    "tests/test_e.py": "from ws import e\n",
    "tests/test_cli.py": "",
    "tests/test_cli_registry.py": "",
    "README.md": "hi\n",
    "src/ws/pk/__init__.py": "import importlib\n\nimportlib.import_module('ws.pk.leaf')\n",
    "src/ws/pk/leaf.py": "Q = 1\n",
    "tests/test_pk.py": "from ws import pk\n",
    **{f"tests/test_d{i}.py": "from ws import d\n" for i in range(41)},
}


def run_selected(
    tmp_path: Path,
    change: dict[str, str | None],
    extra_env: dict[str, str] | None = None,
    selector: bool = True,
    fake_uv: bool = True,
    expect_rc: int = 0,
    expect_log: bool = True,
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
        cfg = ["-c", "user.name=t", "-c", "user.email=noreply", "-c", "commit.gpgsign=false"]
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
    assert done.returncode == expect_rc, done.stderr
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [c for c in lines if c.startswith("uv ")], done.stderr


def test_a_one_module_change_runs_its_tests_only(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"})
    assert calls[-1] == f"{SUITE} tests/test_c.py"
    assert "1 test file(s)" in err


def test_a_module_change_runs_the_tests_of_what_imports_it(tmp_path):
    calls, _ = run_selected(tmp_path, {"src/ws/a.py": "X = 2\n"})
    assert calls[-1] == f"{SUITE} tests/test_a.py tests/test_b.py"


def test_a_module_a_package_finds_by_scanning_selects_the_tests_that_import_the_package(tmp_path):
    calls, _ = run_selected(tmp_path, {"src/ws/pk/leaf.py": "Q = 2\n"})
    assert calls[-1] == f"{SUITE} tests/test_pk.py"


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
    ],
)
def test_a_path_that_reaches_every_test_runs_the_capped_set_not_the_suite(tmp_path, path):
    calls, err = run_selected(tmp_path, {path: "x = 1\n"})
    assert calls[-1] == CAPPED_SMOKE
    assert "CAPPED" in err


def test_the_capped_set_adds_changed_tests_and_direct_importers(tmp_path):
    calls, _ = run_selected(
        tmp_path, {"pyproject.toml": "x = 1\n", "src/ws/a.py": "X = 2\n", "tests/test_c.py": "# c\n"}
    )
    # test_a imports a; test_b only reaches a through b (one hop, not the closure); test_c changed
    assert (
        calls[-1]
        == f"{SUITE} tests/test_a.py tests/test_c.py tests/test_cli.py tests/test_cli_registry.py tests/test_prepush_hook.py -x"
    )


def test_a_deleted_file_runs_the_capped_set(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": None})
    assert calls[-1] == CAPPED_SMOKE
    assert "renamed or deleted" in err


def test_more_than_forty_affected_files_run_the_capped_set(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/d.py": "Z = 2\n"})
    assert "CAPPED" in err
    # 41 tests import d directly; test_e reaches it through e only, so it is left to CI
    assert "tests/test_e.py" not in calls[-1]
    assert "tests/test_d0.py" in calls[-1]


def test_nothing_to_select_runs_the_smoke_set_never_zero(tmp_path):
    calls, err = run_selected(tmp_path, {"README.md": "changed\n"})
    assert calls[-1] == SMOKE_RUN
    assert "SMOKE" in err


def test_a_selector_that_errors_runs_the_named_tests_and_the_smoke_set(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "def broken(:\n"})
    assert calls[-1] == f"{SUITE} tests/test_c.py {SMOKE_ARGS.removeprefix(SUITE + ' ')} -x"
    assert "no usable selection" in err


def test_no_selector_in_the_checkout_runs_the_named_tests_and_the_smoke_set(tmp_path):
    calls, err = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"}, selector=False)
    assert calls[-1] == f"{SUITE} tests/test_c.py {SMOKE_ARGS.removeprefix(SUITE + ' ')} -x"
    assert "no usable selection" in err


def test_a_forced_full_run_is_logged_and_runs_the_whole_suite(tmp_path):
    calls, _ = run_selected(tmp_path, {"src/ws/c.py": "Y = 2\n"}, {"WS_PREPUSH_FULL": "1"})
    assert calls[-1] == SUITE
    assert "WS_PREPUSH_FULL=1" in (tmp_path / "repo" / ".git" / "ws-prepush-skips.log").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
def test_a_capped_set_that_hits_the_cap_fails_the_push(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text("#!/usr/bin/env bash\nexec sleep 20\n", encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    _, err = run_selected(
        tmp_path, {"pyproject.toml": "x = 1\n"}, {"WS_PREPUSH_CAP": "1"}, fake_uv=False, expect_rc=124, expect_log=False
    )
    assert "is not a pass" in err
    assert "its 1 s cap" in err
    assert "WS_PREPUSH_CAP=<seconds>" in err


@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
def test_the_cap_is_300_s_at_least_and_grows_6_s_a_file(tmp_path):
    (tmp_path / "small").mkdir()
    (tmp_path / "big").mkdir()
    (tmp_path / "set").mkdir()
    _, err = run_selected(tmp_path / "small", {"pyproject.toml": "x = 1\n"})
    assert "timeout 300 uv " in err
    new_tests = {f"tests/test_new{i}.py": "def test_x():\n    pass\n" for i in range(70)}
    _, err = run_selected(tmp_path / "big", {"pyproject.toml": "x = 1\n", **new_tests})
    assert "timeout 438 uv " in err  # 3 smoke + 70 new files = 73 files
    _, err = run_selected(tmp_path / "set", {"pyproject.toml": "x = 1\n", **new_tests}, {"WS_PREPUSH_CAP": "77"})
    assert "timeout 77 uv " in err


@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5", "10s"])
def test_a_bad_cap_warns_and_uses_the_computed_one(tmp_path, bad):
    calls, err = run_selected(tmp_path, {"pyproject.toml": "x = 1\n"}, {"WS_PREPUSH_CAP": bad})
    assert f"WS_PREPUSH_CAP='{bad}' is not a whole number" in err
    assert "timeout 300 uv " in err
    assert calls[-1] == CAPPED_SMOKE


@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
def test_the_cap_stops_growing_at_1800_s(tmp_path):
    new_tests = {f"tests/test_new{i}.py": "def test_x():\n    pass\n" for i in range(320)}
    _, err = run_selected(tmp_path, {"pyproject.toml": "x = 1\n", **new_tests})
    assert "timeout 1800 uv " in err


@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
def test_time_queued_in_the_wrapper_does_not_count_against_the_cap(tmp_path):
    # a wrapper that waits 2 s (a busy queue) before it runs the tests, with a 1 s cap: the tests are quick, so it passes
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "slow-wrap").write_text('#!/usr/bin/env bash\nsleep 2\nexec "$@"\n', encoding="utf-8")
    (bin_dir / "slow-wrap").chmod(0o755)
    calls, _ = run_selected(
        tmp_path, {"pyproject.toml": "x = 1\n"}, {"WS_PREPUSH_CAP": "1", "WS_PREPUSH_WRAP": "slow-wrap"}
    )
    assert calls[-1] == CAPPED_SMOKE


def _selector_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("affected_tests_under_test", SELECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    git = shutil.which("git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    cfg = ["-c", "user.name=t", "-c", "user.email=noreply", "-c", "commit.gpgsign=false"]
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run([git, "init", "-q", "-b", "work", str(repo)], check=True, env=env)
    subprocess.run([git, "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run([git, "-C", str(repo), *cfg, "commit", "-q", "-m", "base"], check=True, env=env)
    return repo


def _commit_change(repo: Path, edit) -> None:
    git = shutil.which("git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    cfg = ["-c", "user.name=t", "-c", "user.email=noreply", "-c", "commit.gpgsign=false"]
    subprocess.run([git, "-C", str(repo), "branch", "base"], check=True, env=env)
    edit(repo)
    subprocess.run([git, "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run([git, "-C", str(repo), *cfg, "commit", "-q", "-m", "change"], check=True, env=env)


def test_changed_reads_a_rename_a_copy_and_odd_paths_whole(tmp_path):
    body = "line one\nline two\nline three\nline four\nline five\n" * 3
    repo = _repo_with(tmp_path, {"src/ws/old.py": body, "src/ws/src.py": body + "x\n", "tests/test_a.py": ""})

    def edit(r: Path) -> None:
        (r / "src/ws/old.py").rename(r / "src/ws/new.py")
        (r / "src/ws/copy.py").write_text(body + "x\n", encoding="utf-8")
        (r / 'tests/test_odd name "q".py').write_text("", encoding="utf-8")

    _commit_change(repo, edit)
    at = _selector_module()
    at.ROOT = repo
    mod, added, gone = at.changed("base")
    assert "src/ws/new.py" in mod and gone == ["src/ws/old.py"]
    assert "src/ws/copy.py" in added
    assert 'tests/test_odd name "q".py' in added
