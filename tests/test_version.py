"""`ws version`: the JSON shape, the git facts of a checkout, the release stamp, and the install-source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from ws import cli, version
from ws.output import SCHEMA

FIELDS = {"version", "git_sha", "dirty", "build_date", "python", "install_path", "install_source"}
GIT = shutil.which("git")


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("WS_OUTPUT", raising=False)


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def fake_checkout(root: Path) -> Path:
    pkg = root / "src" / "ws"
    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return pkg


def test_json_uses_the_output_contract(capsys):
    rc, out = run_cli(capsys, "version", "--json")
    env = json.loads(out)
    assert rc == 0
    assert (env["schema"], env["kind"], env["status"]) == (SCHEMA, "version", "ok")
    assert set(env["data"]) == FIELDS
    assert env["data"]["version"] == cli.__version__
    assert env["data"]["python"].count(".") >= 1
    assert Path(env["data"]["install_path"]).name == "ws"


def test_text_lists_every_field_and_dash_dash_version_stays_one_line(capsys):
    rc, out = run_cli(capsys, "version")
    assert rc == 0
    for field in FIELDS:
        assert field in out
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"ws {cli.__version__}"


def test_a_stamped_build_wins_over_git(monkeypatch):
    stamp = types.SimpleNamespace(git_sha="a" * 40, dirty=False, build_date="2026-11-01T00:00:00Z", source="release")
    monkeypatch.setitem(sys.modules, "ws._build", stamp)
    monkeypatch.setattr(version, "git_info", lambda root: pytest.fail("a stamped build must not ask git"))
    data = version.collect()
    assert data["git_sha"] == "a" * 40
    assert data["dirty"] is False
    assert data["build_date"] == "2026-11-01T00:00:00Z"


def test_a_partial_stamp_falls_back_to_unknown(monkeypatch):
    monkeypatch.setitem(sys.modules, "ws._build", types.SimpleNamespace())
    data = version.collect()
    assert data["git_sha"] == "unknown"
    assert data["build_date"] == "unknown"


def test_no_stamp_and_no_checkout_is_unknown_not_a_guess(monkeypatch, tmp_path):
    site = tmp_path / "venv" / "lib" / "site-packages" / "ws"
    site.mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "ws._build", None)  # an import of it raises ImportError
    monkeypatch.setattr(version, "package_dir", lambda: site)
    monkeypatch.setattr(version, "direct_url", lambda: None)
    monkeypatch.setattr(version, "git_info", lambda root: pytest.fail("not a checkout: git must not be asked"))
    data = version.collect()
    assert (data["git_sha"], data["dirty"], data["build_date"]) == ("unknown", None, "unknown")
    assert data["install_source"] == "unknown"


@pytest.mark.skipif(GIT is None, reason="needs git")
def test_checkout_reads_git_at_run_time(monkeypatch, tmp_path):
    pkg = fake_checkout(tmp_path / "repo")
    root = pkg.parent.parent

    def git(*args: str) -> str:
        env = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        }
        done = subprocess.run(
            [
                GIT,
                "-C",
                str(root),
                "-c",
                "user.name=t",
                "-c",
                "user.email=none",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return done.stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    sha = git("rev-parse", "HEAD")
    monkeypatch.setitem(sys.modules, "ws._build", None)
    monkeypatch.setattr(version, "package_dir", lambda: pkg)
    data = version.collect()
    assert data["git_sha"] == sha
    assert data["dirty"] is False
    assert data["install_source"] == "checkout"
    (root / "scratch.txt").write_text("x", encoding="utf-8")
    assert version.collect()["dirty"] is True


def test_git_missing_gives_unknown_sha_and_null_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(version.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert version.git_info(tmp_path) == {"git_sha": "unknown", "dirty": None, "build_date": "unknown"}


def test_the_real_checkout_reports_its_own_head(capsys):
    if version.source_tree(version.package_dir()) is None:
        pytest.skip("not running from a git checkout")
    rc, out = run_cli(capsys, "version", "--json")
    data = json.loads(out)["data"]
    assert rc == 0
    assert data["install_source"] == "checkout"
    assert data["git_sha"] == "unknown" or len(data["git_sha"]) == 40


@pytest.mark.parametrize(
    ("durl", "has_build", "prefix", "expected"),
    [
        ({"url": "file:///t", "dir_info": {"editable": True}}, False, "/x", "checkout"),
        ({"url": "git+https://example.invalid/r", "vcs_info": {"vcs": "git"}}, False, "/x", "checkout"),
        ({"url": "file:///t/ws.whl", "archive_info": {"hash": "sha256=ab"}}, True, "/x", "release"),
        (None, True, "/home/u/.local/share/uv/tools/ws-aitoolkit", "pypi"),  # a stamped wheel from an index
        (None, False, "/home/u/.local/share/uv/tools/ws-aitoolkit", "uv-tool"),
        (None, False, "/home/u/.local/pipx/venvs/ws-aitoolkit", "pipx"),
        (None, False, "C:\\Users\\u\\pipx\\venvs\\ws-aitoolkit", "pipx"),
        (None, False, "/somewhere/else", "unknown"),
        ({"url": "file:///t/ws.whl", "archive_info": {}}, False, "/home/u/.local/pipx/venvs/ws", "release"),
        ({"url": "https://example.invalid/x"}, False, "/home/u/.local/pipx/venvs/ws", "pipx"),
    ],
)
def test_install_source_rules(tmp_path, durl, has_build, prefix, expected):
    site = tmp_path / "site-packages" / "ws"
    site.mkdir(parents=True)
    assert version.install_source(site, has_build, durl, prefix) == expected


def test_a_source_tree_is_a_checkout_whatever_pip_says(tmp_path):
    pkg = fake_checkout(tmp_path / "repo")
    assert version.source_tree(pkg) is None  # no .git yet: not taken for a checkout
    assert version.install_source(pkg, False, None, "/x") == "unknown"
    (pkg.parent.parent / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a worktree's .git is a file
    assert version.source_tree(pkg) == pkg.parent.parent
    assert version.install_source(pkg, True, None, "/x") == "checkout"


def test_direct_url_is_read_from_the_installed_metadata(monkeypatch):
    class Dist:
        def __init__(self, text):
            self.text = text

        def read_text(self, name):
            assert name == "direct_url.json"
            return self.text

    monkeypatch.setattr(version.metadata, "distribution", lambda name: Dist('{"url": "file:///x", "archive_info": {}}'))
    assert version.direct_url() == {"url": "file:///x", "archive_info": {}}
    monkeypatch.setattr(version.metadata, "distribution", lambda name: Dist("not json"))
    assert version.direct_url() is None
    monkeypatch.setattr(version.metadata, "distribution", lambda name: Dist(None))
    assert version.direct_url() is None
    monkeypatch.setattr(version.metadata, "distribution", lambda name: Dist("[1]"))
    assert version.direct_url() is None
