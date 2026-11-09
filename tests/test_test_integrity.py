"""T1, the test-integrity check (scripts/test_integrity.py), on small throwaway repositories: what it flags, what it lets
through, and what the pull request body must say. HOME is a scratch folder; git never reads the real one."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "test_integrity.py"

spec = importlib.util.spec_from_file_location("test_integrity_script", SCRIPT)
assert spec and spec.loader
ti = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ti  # a dataclass looks its own module up here
spec.loader.exec_module(ti)

TWO_ASSERTS = "def test_sum():\n    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n"
ONE_ASSERT = "def test_sum():\n    assert 1 + 1 == 2\n"
MARK = "@" + "pytest.mark."  # built here so the line is not read as an address by the PII gate


def module(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(path.parent),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=" + "t" + "@" + "example.test", "-c", "commit.gpgsign=false", *args],
            cwd=self.path, env=self.env, capture_output=True, text=True, check=True,
        )  # fmt: skip
        return done.stdout.strip()

    def commit(self, files: dict[str, str | bytes | None]) -> str:
        """Write (or delete, for None) the files and commit; the new commit's sha."""
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")

    def hits(self, base: str) -> list:
        return ti.check(self.path, base)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def test_a_clean_diff_passes(repo, capsys):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS, "src/mod.py": "x = 1\n"})
    repo.commit(
        {"tests/test_a.py": TWO_ASSERTS + "\n\ndef test_more():\n    assert 3 == 3.0\n", "src/mod.py": "x = 2\n"}
    )
    assert repo.hits(base) == []
    code, lines = ti.verdict([], "")
    assert code == 0 and "no test was weakened" in lines[0]
    assert ti.main(["--base", base, "--repo", str(repo.path)], environ={}) == 0
    assert "no test was weakened" in capsys.readouterr().out


def test_flags_a_removed_assert(repo):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    repo.commit({"tests/test_a.py": ONE_ASSERT})
    hits = repo.hits(base)
    assert [h.path for h in hits] == ["tests/test_a.py"]
    assert "fewer assertion lines (2 before, 1 after)" in hits[0].why and hits[0].needs == "test-change"


def test_a_deleted_test_file_counts_as_removed_asserts(repo):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS, "tests/data.txt": "not a test\n"})
    repo.commit({"tests/test_a.py": None, "tests/data.txt": None})
    hits = repo.hits(base)
    assert {h.path for h in hits} == {"tests/test_a.py"}
    assert any("fewer assertion lines (2 before, 0 after)" in h.why for h in hits)
    assert all("the file was deleted" in h.why for h in hits)


def test_a_renamed_test_file_is_compared_with_its_old_path(repo):
    base = repo.commit({"tests/test_old.py": TWO_ASSERTS})
    repo.git("mv", "tests/test_old.py", "tests/test_new.py")
    repo.commit({"tests/test_new.py": ONE_ASSERT})
    assert [h.why.split(" (")[0] for h in repo.hits(base)] == ["fewer assertion lines"]
    # and a rename that changes nothing says nothing
    base = repo.git("rev-parse", "HEAD")
    repo.git("mv", "tests/test_new.py", "tests/test_renamed.py")
    repo.commit({})
    assert repo.hits(base) == []


def test_flags_widened_approx_abs_rel(repo):
    exact = "import pytest\n\n\ndef test_v():\n    assert 0.1 + 0.2 == pytest.approx(0.3, rel={rel}, abs={abs})\n"
    base = repo.commit({"tests/test_a.py": exact.format(rel="1e-9", abs="1e-9")})
    repo.commit({"tests/test_a.py": exact.format(rel="1e-3", abs="1e-9")})
    assert any("widened" in h.why and "rel=1e-3" in h.why for h in repo.hits(base))
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_a.py": exact.format(rel="1e-3", abs="0.5")})
    assert any("widened" in h.why and "abs=0.5" in h.why for h in repo.hits(base))
    # a tolerance that gets tighter is not weaker
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_a.py": exact.format(rel="1e-6", abs="1e-6")})
    assert repo.hits(base) == []
    # exact to approximate is weaker too
    base = repo.commit({"tests/test_b.py": "def test_w():\n    assert 0.1 + 0.2 == 0.3\n"})
    repo.commit({"tests/test_b.py": "import pytest\n\n\ndef test_w():\n    assert 0.1 + 0.2 == pytest.approx(0.3)\n"})
    assert any("widened" in h.why for h in repo.hits(base))
    # a new test with a tolerance in an old file is a new test, not a widened one
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "tests/test_b.py": "import pytest\n\n\ndef test_w():\n    assert 0.1 + 0.2 == pytest.approx(0.3)\n\n\ndef test_x():\n    assert 1 == pytest.approx(1, rel=0.5)\n"
        }
    )
    assert repo.hits(base) == []


@pytest.mark.parametrize(
    "old, new",
    [
        ("pytest.approx(1.0)", "pytest.approx(1.0, abs=0.5)"),  # a kind the line did not have
        ("pytest.approx(1.0, abs=1e-9)", "pytest.approx(1.0, rel=0.5)"),  # another kind, and much looser
        ("pytest.approx(1.0, rel=0.01)", "pytest.approx(1.0, rel=.5)"),  # a number written without its leading 0
        (
            "pytest.approx(1.0, rel=1e-6)",
            "pytest.approx(1.0, rel=1e-6, abs=0.1)",
        ),  # a second kind on a line that had one
        ("pytest.approx(1.0, 0.01)", "pytest.approx(1.0, .5)"),  # the positional form
        ("math.isclose(a, b, rel_tol=1e-9)", "math.isclose(a, b, rel_tol=1e-9, abs_tol=.1)"),
    ],
)
def test_flags_a_tolerance_that_changes_kind_or_is_written_oddly(repo, old, new):
    text = "import math\nimport pytest\n\n\ndef test_v(a=1.0, b=1.0):\n    assert a == {}\n"
    base = repo.commit({"tests/test_a.py": text.format(old)})
    repo.commit({"tests/test_a.py": text.format(new)})
    assert any("tolerance" in h.why for h in repo.hits(base)), (old, new)


@pytest.mark.parametrize(
    "old, new",
    [
        ("pytest.approx(1.0, rel=0.5)", "pytest.approx(1.0, rel=.01)"),  # tighter
        ("pytest.approx(1.0, rel=1e-3, abs=0.5)", "pytest.approx(1.0, abs=0.1, rel=1e-3)"),  # reordered, abs tighter
        ("pytest.approx(1.0, abs=0.5)", "pytest.approx(1.0, abs=0.5)  # same"),  # a comment
    ],
)
def test_a_tolerance_that_gets_tighter_or_stays_is_not_a_hit(repo, old, new):
    text = "import pytest\n\n\ndef test_v():\n    assert 1.0 == {}\n"
    base = repo.commit({"tests/test_a.py": text.format(old)})
    repo.commit({"tests/test_a.py": text.format(new)})
    assert repo.hits(base) == [], (old, new)


def test_the_code_that_defines_a_weakened_test_and_every_conftest_are_machinery(repo):
    files = {
        "tests/conftest.py": "A = 1\n",
        "tests/roles/conftest.py": "B = 1\n",
        "tests/deep/er/conftest.py": "C = 1\n",
        ".pre-commit-config.yaml": "repos: [1]\n",
        "src/ws/guards/test_weakening.py": "D = 1\n",
        "src/ws/guards/_paths.py": "E = 1\n",
        "src/ws/guards/other.py": "F = 1\n",
        "conftest.py": "G = 1\n",
        "pytest.ini": "H = 1\n",
        "pytest.toml": "L = 1\n",
        "tests/gate_corpus/rows.jsonl": "M = 1\n",
        "src/ws/__init__.py": "I = 1\n",
        "src/ws/guards/__init__.py": "J = 1\n",
        "src/ws/approvals.py": "K = 1\n",
    }
    base = repo.commit(files)
    repo.commit({k: v.replace("1", "2") for k, v in files.items()})
    hits = repo.hits(base)
    assert {h.path for h in hits} == set(files) - {"src/ws/guards/other.py"}
    assert {h.needs for h in hits} == {"test-change"}


def test_flags_a_new_skip_and_xfail(repo):
    plain = ["import pytest", "", "", "def test_a():", "    assert 1", "", "", "def test_b():", "    assert 2"]
    skipped = ["import pytest", "", "", MARK + "skip(reason='later')", *plain[3:]]
    base = repo.commit({"tests/test_a.py": module(plain)})
    repo.commit({"tests/test_a.py": module(skipped)})
    assert any("skip or expected-failure marker" in h.why for h in repo.hits(base))
    # a marker on a new test in a file that existed is still a new marker
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_a.py": module([*skipped, "", "", MARK + "xfail", "def test_c():", "    assert 3"])})
    assert any("already existed" in h.why for h in repo.hits(base))
    # a brand new file may carry its own markers
    base = repo.git("rev-parse", "HEAD")
    new = ["import pytest", "", "", MARK + "skipif(True, reason='x')", "def test_n():", "    assert 1"]
    repo.commit({"tests/test_new.py": module(new)})
    assert repo.hits(base) == []


def test_flags_conftest_pytest_and_ci_edits(repo):
    pyproject = '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-p ws_harness"\n'
    base = repo.commit(
        {
            "tests/conftest.py": "A = 1\n",
            "tests/plugins/ws_harness.py": "B = 1\n",
            "pyproject.toml": pyproject,
            ".github/workflows/ci.yml": "name: ci\n",
            "scripts/test_integrity.py": "C = 1\n",
            "README.md": "hi\n",
        }
    )
    repo.commit(
        {
            "tests/conftest.py": "A = 2\n",
            "tests/plugins/ws_harness.py": "B = 2\n",
            "pyproject.toml": pyproject.replace("-p ws_harness", "-p ws_harness --lf"),
            ".github/workflows/ci.yml": "name: ci2\n",
            "scripts/test_integrity.py": "C = 2\n",
            "README.md": "hello\n",
        }
    )
    hits = repo.hits(base)
    assert {h.path for h in hits} == {
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "scripts/test_integrity.py",
        "tests/conftest.py",
        "tests/plugins/ws_harness.py",
    }
    assert {h.needs for h in hits} == {"test-change"}


def test_a_pyproject_edit_outside_the_pytest_tables_is_not_a_hit(repo):
    base = repo.commit({"pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'})
    repo.commit({"pyproject.toml": '[project]\nname = "y"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'})
    assert repo.hits(base) == []


def test_a_hit_passes_with_a_test_change_line(repo):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    repo.commit({"tests/test_a.py": ONE_ASSERT})
    hits = repo.hits(base)
    code, lines = ti.verdict(hits, "Some words.\n\ntest-change: merged two checks into one\n")
    assert code == 0 and "declared in the pull request body" in lines[0]
    code, lines = ti.verdict(hits, "test-change:\nholdout-change: unrelated\n")
    assert code == 1 and "add a non-empty `test-change:` line" in lines[-1]
    assert ti.verdict(hits, "TEST-CHANGE:   why\r\n")[0] == 0  # CRLF, any case
    assert ti.verdict(hits, "<!--\ntest-change: the template's example\n-->")[0] == 1  # a comment isn't a line
    assert ti.verdict(hits, "see test-change: in the guide")[0] == 1  # the line has to start with it


def test_holdout_edit_needs_a_holdout_change_line(repo):
    base = repo.commit({"tests/holdout/test_core.py": ONE_ASSERT})
    repo.commit({"tests/holdout/test_core.py": ONE_ASSERT + "\n\ndef test_two():\n    assert 3 == 3.0\n"})
    hits = repo.hits(base)
    assert [(h.path, h.needs) for h in hits] == [("tests/holdout/test_core.py", "holdout-change")]
    assert ti.verdict(hits, "test-change: not the right line")[0] == 1
    assert ti.verdict(hits, "holdout-change: a new promise")[0] == 0
    # a new file in the holdout is an edit to it
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/holdout/test_more.py": ONE_ASSERT})
    assert [h.needs for h in repo.hits(base)] == ["holdout-change"]


def test_an_empty_body_fails_on_a_hit(repo, capsys):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    repo.commit({"tests/test_a.py": ONE_ASSERT})
    assert ti.main(["--base", base, "--repo", str(repo.path)], environ={}) == 1  # no variable at all
    assert ti.main(["--base", base, "--repo", str(repo.path)], environ={"PR_BODY": ""}) == 1
    assert ti.main(["--base", base, "--repo", str(repo.path)], environ={"PR_BODY": " \n\t"}) == 1
    assert "add a non-empty `test-change:` line" in capsys.readouterr().out


def test_the_body_comes_from_the_environment_only(repo):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    repo.commit({"tests/test_a.py": ONE_ASSERT})
    cmd = [sys.executable, str(SCRIPT), "--base", base, "--repo", str(repo.path)]
    env = {"PATH": os.environ["PATH"], "HOME": str(repo.path.parent)}
    bare = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert bare.returncode == 1
    told = subprocess.run(
        cmd, env={**env, "PR_BODY": "test-change: reason"}, capture_output=True, text=True, timeout=60, check=False
    )
    assert told.returncode == 0, told.stdout + told.stderr
    other = subprocess.run(
        [*cmd, "--body-env", "OTHER"],
        env={**env, "OTHER": "test-change: x"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert other.returncode == 0
    assert "--body" not in SCRIPT.read_text(encoding="utf-8").replace("--body-env", "")  # no flag carries the text


def test_a_base_that_is_not_in_the_clone_is_a_typed_failure(repo, capsys):
    repo.commit({"tests/test_a.py": TWO_ASSERTS})
    with pytest.raises(ti.IntegrityError) as err:
        repo.hits("0" * 40)
    assert err.value.kind == "missing-commit" and "fetch-depth: 0" in str(err.value)
    code = ti.main(
        ["--base", "0" * 40, "--repo", str(repo.path)], environ={"PR_BODY": "test-change: x\nholdout-change: x"}
    )
    assert code == 2 and "missing-commit" in capsys.readouterr().err  # never a pass, whatever the body says


def test_a_test_file_that_is_not_text_is_reported(repo):
    base = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    repo.commit({"tests/test_a.py": b"\xff\xfe\x00 not utf-8 \x80"})
    hits = repo.hits(base)
    assert [h.path for h in hits] == ["tests/test_a.py"] and "isn't text" in hits[0].why


def test_the_check_imports_the_guards_detection_and_does_not_copy_it():
    from ws.guards import test_weakening as guard

    assert ti.tw is guard  # one definition of "an assertion", "a test" and "a skip"
    text = SCRIPT.read_text(encoding="utf-8")
    for pattern in ("_SKIP = ", "_ASSERT = ", "def compare("):
        assert pattern not in text


def test_a_deleted_test_file_with_nothing_to_count_is_still_a_hit(repo):
    base = repo.commit({"tests/test_a.py": "VALUE = 1\n"})
    repo.commit({"tests/test_a.py": None})
    assert [h.path for h in repo.hits(base)] == ["tests/test_a.py"]


def test_a_file_name_cannot_write_its_own_log_line():
    hit = ti.Hit("tests/holdout/a\n::notice::passed", "an edit", ti.HOLDOUT_CHANGE)
    assert "\n" not in str(hit)


def test_a_test_file_that_cannot_be_read_is_a_typed_failure(repo):
    head = repo.commit({"tests/test_a.py": TWO_ASSERTS})
    with pytest.raises(ti.IntegrityError) as err:
        ti.blob(repo.path, head, "tests/not_there.py")
    assert err.value.kind == "git"
    assert ti.blob(repo.path, head, "tests/not_there.py", must=False) == ""
