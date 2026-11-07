"""release.yml: which tags it accepts, how it writes the notes for a release candidate, and that a candidate is marked.

The workflow is YAML and ws is stdlib only, so these tests read the file as text, pull out the shell of the two steps
that decide things (the tag check and the release notes) and run that shell on a temporary CHANGELOG. They do not
reach GitHub.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None or shutil.which("awk") is None, reason="needs bash and awk")


def step_script(name_start: str) -> str:
    """The `run: |` text of the step whose name starts with `name_start`, with the YAML indent removed."""
    lines = WORKFLOW.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"- name: {name_start}"))
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body: list[str] = []
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def run_bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {"PATH": os.environ["PATH"], "HOME": str(cwd), **env}
    return subprocess.run(
        [BASH, "-e", "-c", script], cwd=cwd, env=full, capture_output=True, text=True, check=False, timeout=60
    )


def tag_check(tag: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = step_script("the tag is vX.Y.Z or vX.Y.ZrcN")
    return run_bash(script, tmp_path, {"TAG": tag, "GITHUB_EVENT_NAME": "push"})


@pytest.mark.parametrize("tag", ["v0.1.8", "v10.20.30", "v0.2.0rc1", "v0.2.0rc12"])
def test_stable_and_release_candidate_tags_are_accepted(tag: str, tmp_path: Path):
    assert tag_check(tag, tmp_path).returncode == 0


@pytest.mark.parametrize(
    "tag",
    [
        "0.2.0",
        "v0.2",
        "v0.2.0rc",
        "v0.2.0-rc1",
        "v0.2.0b1",
        "v0.2.0a1",
        "v0.2.0.dev1",
        "v0.2.0rc1.post1",
        "v0.2.0+local",
        "",
    ],
)
def test_other_tags_are_refused(tag: str, tmp_path: Path):
    r = tag_check(tag, tmp_path)
    assert r.returncode == 1
    assert "is not vX.Y.Z or vX.Y.ZrcN" in r.stdout


CHANGELOG = """# Changelog

## [Unreleased]

Work in progress.

## [0.2.0] - unreleased

The stable notes.

## [0.1.9] - 2026-01-01

Older.

[Unreleased]: https://example.invalid/compare
"""


def notes(version: str, prerelease: str, tmp_path: Path, changelog: str = CHANGELOG, event: str = "push"):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    script = step_script("release notes from the CHANGELOG section for this version")
    r = run_bash(script, tmp_path, {"VERSION": version, "PRERELEASE": prerelease, "GITHUB_EVENT_NAME": event})
    return r, tmp_path / "release-notes.md"


def test_a_candidate_uses_the_section_of_the_release_it_leads_to(tmp_path: Path):
    r, out = notes("0.2.0rc1", "true", tmp_path)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "release candidate for testing" in text
    assert "--prerelease allow" in text
    assert "The stable notes." in text
    assert "Older." not in text


def test_a_candidate_with_its_own_section_uses_it(tmp_path: Path):
    changelog = CHANGELOG.replace(
        "## [0.2.0] - unreleased", "## [0.2.0rc1] - 2026-02-02\n\nThe candidate notes.\n\n## [0.2.0] - unreleased"
    )
    r, out = notes("0.2.0rc1", "true", tmp_path, changelog)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "The candidate notes." in text and "The stable notes." not in text


def test_a_candidate_falls_back_to_unreleased_and_may_still_say_unreleased(tmp_path: Path):
    changelog = CHANGELOG.replace("## [0.2.0] - unreleased\n\nThe stable notes.\n\n", "")
    r, out = notes("0.2.0rc1", "true", tmp_path, changelog)
    assert r.returncode == 0, r.stdout
    assert "Work in progress." in out.read_text(encoding="utf-8")


def test_a_candidate_with_nothing_to_say_is_refused(tmp_path: Path):
    r, _ = notes("0.3.0rc1", "true", tmp_path, "# Changelog\n\n## [Unreleased]\n\n## [0.1.9] - 2026-01-01\n\nOlder.\n")
    assert r.returncode == 1
    assert "no '## [0.3.0rc1]' section" in r.stdout


def test_a_stable_release_still_needs_its_own_dated_section(tmp_path: Path):
    r, _ = notes("0.2.0", "false", tmp_path)
    assert r.returncode == 1  # the section still says "unreleased"
    assert "put the date in" in r.stdout
    dated = CHANGELOG.replace("## [0.2.0] - unreleased", "## [0.2.0] - 2026-02-03")
    r, out = notes("0.2.0", "false", tmp_path, dated)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "The stable notes." in text and "release candidate" not in text


def test_a_stable_release_does_not_borrow_unreleased(tmp_path: Path):
    r, _ = notes("0.9.0", "false", tmp_path)
    assert r.returncode == 1
    assert "no '## [0.9.0]' section" in r.stdout


def test_the_prerelease_flag_follows_the_version_and_reaches_the_release_and_the_pass_test():
    assert re.search(r'case "\$version" in \*rc\*\) prerelease=true ;; \*\) prerelease=false ;; esac', WORKFLOW)
    assert "prerelease: ${{ needs.build.outputs.prerelease }}" in WORKFLOW  # the GitHub release is marked
    assert 'if [ "$PRERELEASE" = true ]; then latest=false; else latest=true; fi' in WORKFLOW  # pass test by tag
    assert 'data["channel"] == ("rc" if sys.argv[4] == "true" else "stable")' in WORKFLOW


def test_the_pypi_job_uploads_a_candidate_like_any_release():
    pypi = WORKFLOW[WORKFLOW.index("  pypi:") : WORKFLOW.index("  # The pass test")]
    assert "if: github.event_name == 'push'" in pypi
    assert "prerelease" not in pypi  # PyPI reads the pre-release from the rcN version itself
    assert "environment: pypi" in pypi
