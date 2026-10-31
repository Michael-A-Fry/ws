"""install.sh: help, usage errors, the dry run and the preflight messages, and a real run against stub uv/pipx/ws/git.

Every run gets its own PATH holding only stubs plus a few system tools, so the result does not depend on what the
machine running the tests has installed, and nothing is ever installed or fetched.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
BASH = shutil.which("bash")
SYSTEM_TOOLS = ("uname", "grep", "cat", "sed", "sort", "tail")

pytestmark = pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="install.sh needs a POSIX shell")


def stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Box:
    """A throwaway PATH and HOME, with helpers to add stub commands and run the installer."""

    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "calls.log"
        for tool in SYSTEM_TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (self.bin / tool).symlink_to(found)

    def python(self, ok: bool = True) -> None:
        # a python3 that says yes (the real interpreter) or no (older than 3.11) to the version test
        body = (
            f'exec "{sys.executable}" "$@"' if ok else 'case "$*" in *version_info*) exit 1;; esac; echo "Python 3.9.0"'
        )
        stub(self.bin, "python3", body)

    def uv(self, extra: str = "") -> None:
        stub(self.bin, "uv", f'echo "uv $*" >> "{self.log}"\n{extra}\ncase "$1" in --version) echo "uv 0.0.0";; esac')

    def pipx(self) -> None:
        stub(self.bin, "pipx", f'echo "pipx $*" >> "{self.log}"\ncase "$1" in --version) echo "0.0.0";; esac')

    def git(self, *tags: str, fail: bool = False) -> None:
        lines = "".join(f"echo 'abc123\trefs/tags/{t}'\n" for t in tags)
        body = f'echo "git $*" >> "{self.log}"\n' + ("exit 128\n" if fail else lines)
        stub(self.bin, "git", body)

    def ws(self, doctor_exit: int = 1, version: str = "9.9.9") -> None:
        stub(
            self.bin,
            "ws",
            f'echo "ws $*" >> "{self.log}"\ncase "$1" in --version) echo "ws {version}";; doctor) echo "WS001 ok";'
            f" exit {doctor_exit};; esac",
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def env(self) -> dict[str, str]:
        return {"PATH": str(self.bin), "HOME": str(self.home), "TERM": "dumb"}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [BASH, str(INSTALL_SH), *args]
        return subprocess.run(cmd, check=False, env=self.env(), capture_output=True, text=True, timeout=60)

    def piped(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the script the way `curl ... | bash -s -- ARGS` does: on stdin."""
        cmd = [BASH, "-s", "--", *args]
        return subprocess.run(
            cmd, check=False, env=self.env(), input=script, capture_output=True, text=True, timeout=60
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def ready(box: Box, *tags: str) -> None:
    """python, uv and a git that lists the given release tags."""
    box.python()
    box.uv()
    box.git(*tags)


def test_help_prints_usage_and_exits_0(box: Box):
    r = box.run("--help")
    assert r.returncode == 0
    for flag in ("--version", "--pypi", "--from", "--no-verify", "--dry-run", "--help"):
        assert flag in r.stdout
    assert "curl -fsSL" in r.stdout and "| bash -s --" in r.stdout
    assert box.calls() == []


def test_no_colour_codes_when_not_a_terminal(box: Box):
    ready(box, "v0.1.0")
    r = box.run("--dry-run")
    assert "\x1b" not in r.stdout + r.stderr


def test_unknown_argument_exits_2_with_usage(box: Box):
    r = box.run("--frobnicate")
    assert r.returncode == 2
    assert "unknown argument: --frobnicate" in r.stderr
    assert "Usage:" in r.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("--version",),
        ("--version", ""),
        ("--version", "v1;touch x"),
        ("--version", "$(id)"),
        ("--version", "v1..2"),
        ("--version", "..", "--dry-run"),
        ("--version", "v1.0.0", "--pypi"),
    ],
)
def test_bad_version_exits_2(box: Box, args: tuple[str, ...]):
    assert box.run(*args).returncode == 2
    assert box.calls() == []


def test_from_installs_a_checkout_without_asking_git(box: Box, tmp_path: Path):
    checkout = tmp_path / "checkout with space"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    box.python()
    box.uv()
    box.ws()  # no git stub at all: a checkout needs neither git nor a tag lookup
    r = box.run("--from", str(checkout))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"uv tool install --force {checkout.resolve()}" in box.calls()
    assert not any(c.startswith("git") for c in box.calls())


@pytest.mark.parametrize("extra", [(), ("--pypi",), ("--version", "v1.0.0")])
def test_from_refuses_a_folder_that_is_not_ws_and_clashing_options(box: Box, tmp_path: Path, extra: tuple[str, ...]):
    ready(box, "v0.1.0")
    folder = tmp_path / "folder"
    folder.mkdir()
    if extra:  # a real checkout, so the clash with the other option is what is refused
        (folder / "pyproject.toml").write_text("", encoding="utf-8")
    assert box.run("--from", str(folder), *extra).returncode == 2
    assert box.run("--from").returncode == 2
    assert not any(c.startswith("uv tool") for c in box.calls())


def test_dry_run_installs_the_latest_release_tag_and_changes_nothing(box: Box):
    ready(box, "v0.2.0", "v0.10.0", "v0.9.1", "v1.0.0-rc1")
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "uv tool install --force git+https://github.com/" in r.stdout
    assert "ws@v0.10.0" in r.stdout  # a version sort, not a text sort; the release candidate is ignored
    assert "ws doctor --offline" in r.stdout
    assert not any(c.startswith("uv tool") for c in box.calls())


def test_no_tag_yet_falls_back_to_main_and_says_so(box: Box):
    ready(box)
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "pre-release" in r.stdout
    assert "ws@main" in r.stdout


def test_version_flag_names_the_tag_without_asking_git_for_tags(box: Box):
    ready(box, "v9.9.9")
    r = box.run("--dry-run", "--version", "v1.2.3")
    assert r.returncode == 0, r.stderr
    assert "ws@v1.2.3" in r.stdout
    assert not any("ls-remote" in c for c in box.calls())


def test_unreachable_tag_list_fails_with_a_hint(box: Box):
    box.python()
    box.uv()
    box.git(fail=True)
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "could not list the release tags" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_github_route_needs_git(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "needs git" in r.stderr


def test_pypi_route_needs_no_git(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run", "--pypi")
    assert r.returncode == 0, r.stderr
    assert "uv tool install --force ws-aitoolkit" in r.stdout


def test_pipx_is_used_when_uv_is_missing_and_gets_the_python_that_was_found(box: Box):
    box.python()
    box.pipx()
    box.git("v0.1.0")
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "pipx install --python python3 --force git+https://github.com/" in r.stdout


def test_no_installer_names_the_exact_install_line_and_troubleshoots(box: Box):
    box.python()
    box.git("v0.1.0")
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_old_python_without_uv_is_refused_with_an_install_line(box: Box):
    box.python(ok=False)
    box.pipx()
    box.git("v0.1.0")
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "Python 3.11 or newer is required" in r.stderr


def test_old_python_with_uv_only_warns(box: Box):
    ready(box, "v0.1.0")
    box.python(ok=False)
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "uv will fetch one" in r.stdout


def test_native_windows_points_at_wsl2(box: Box):
    ready(box, "v0.1.0")
    (box.bin / "uname").unlink()
    stub(box.bin, "uname", 'case "$1" in -m) echo x86_64;; *) echo MINGW64_NT-10.0;; esac')
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "WSL2" in r.stderr
    assert "wsl --install" in r.stderr


def test_real_run_installs_then_smokes_ws_and_accepts_doctor_exit_1(box: Box):
    ready(box, "v0.3.0")
    box.ws(doctor_exit=1)
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(
        c.startswith("uv tool install --force git+https://github.com/") and c.endswith("@v0.3.0") for c in box.calls()
    )
    assert "ws doctor --offline" in box.calls()
    assert "ws --version -> ws 9.9.9" in r.stdout
    assert "ws setup" in r.stdout and "ws doctor" in r.stdout


def test_real_run_fails_when_doctor_does_not_run(box: Box):
    ready(box, "v0.3.0")
    box.ws(doctor_exit=2)
    r = box.run()
    assert r.returncode == 1
    assert "ws doctor did not run properly (exit 2)" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_installed_placeholder_version_fails_even_with_no_verify(box: Box):
    box.python()
    box.uv()
    box.ws(version="0.0.0")
    r = box.run("--pypi", "--no-verify")
    assert r.returncode == 1
    assert "PyPI placeholder" in r.stderr


def test_no_verify_skips_the_smoke_test(box: Box):
    ready(box, "v0.3.0")
    box.ws()
    r = box.run("--no-verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ws doctor --offline" not in box.calls()


def test_ws_found_in_the_uv_bin_folder_when_not_on_path(box: Box, tmp_path: Path):
    uv_bin = tmp_path / "uvbin"
    uv_bin.mkdir()
    stub(uv_bin, "ws", 'case "$1" in --version) echo "ws 9.9.9";; doctor) exit 1;; esac')
    box.python()
    box.git("v0.3.0")
    box.uv(extra=f'case "$*" in "tool dir --bin") echo "{uv_bin}";; esac')
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not on your PATH yet" in r.stdout


def test_install_failure_prints_troubleshooting(box: Box):
    ready(box, "v0.3.0")
    box.uv(extra='case "$1" in tool) exit 7;; esac')
    r = box.run()
    assert r.returncode == 7
    assert "installation failed (exit 7)" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_piped_into_bash_works_and_children_get_no_stdin(box: Box):
    # What `curl ... | bash -s -- --dry-run` does. The comment after main stands for the rest of a script: a child that
    # read stdin would eat it, so the uv and ws stubs report anything they can read.
    reader = f'if read -r line; then echo "STDIN-EATEN $line" >> "{box.log}"; fi'
    box.python()
    box.git("v0.3.0")
    box.uv(extra=reader)
    stub(
        box.bin,
        "ws",
        reader + f'\necho "ws $*" >> "{box.log}"\ncase "$1" in --version) echo "ws 9.9.9"; exit 0;; esac; exit 1',
    )
    script = INSTALL_SH.read_text(encoding="utf-8") + "\n# rest of the script\n"
    r = box.piped(script)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ws doctor ran" in r.stdout
    assert not any("STDIN-EATEN" in c for c in box.calls())


def test_dry_run_piped_into_bash(box: Box):
    ready(box, "v0.3.0")
    r = box.piped(INSTALL_SH.read_text(encoding="utf-8"), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "dry run: not installed" in r.stdout


def test_a_truncated_download_runs_nothing(box: Box):
    ready(box, "v0.3.0")
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == 'main "$@"'
    for cut in (len(lines) - 1, len(lines) // 2, len(lines) // 4):
        r = box.piped("\n".join(lines[:cut]) + "\n")
        assert "[1/5]" not in r.stdout, cut
        assert box.calls() == []


def test_script_is_executable_and_carries_the_full_mit_text():
    assert os.access(INSTALL_SH, os.X_OK)
    text = INSTALL_SH.read_text(encoding="utf-8")
    licence = (ROOT / "LICENSES" / "MIT-claw-code.txt").read_text(encoding="utf-8").splitlines()
    commented = [line[1:].lstrip(" ") for line in text.splitlines()[:40] if line.startswith("#")]
    for line in licence:
        assert not line or line in commented, line
    assert "Copyright (c) 2026 UltraWorkers and Claw Code contributors" in text
