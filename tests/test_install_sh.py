"""install.sh: help, usage errors, the dry run and the preflight messages, and a real run against stub uv/pipx/ws.

Every run gets its own PATH holding only stubs plus uname, grep and cat, so the result does not depend on what the machine
running the tests has installed, and nothing is ever installed.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
BASH = shutil.which("bash")

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
        for tool in ("uname", "grep", "cat"):
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

    def ws(self, doctor_exit: int = 1) -> None:
        stub(
            self.bin,
            "ws",
            f'echo "ws $*" >> "{self.log}"\ncase "$1" in --version) echo "ws 9.9.9";; doctor) echo "WS001 ok";'
            f" exit {doctor_exit};; esac",
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": str(self.bin), "HOME": str(self.home), "TERM": "dumb"}
        return subprocess.run(
            [BASH, str(INSTALL_SH), *args], check=False, env=env, capture_output=True, text=True, timeout=60
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_help_prints_usage_and_exits_0(box: Box):
    r = box.run("--help")
    assert r.returncode == 0
    for flag in ("--version", "--no-verify", "--dry-run", "--help"):
        assert flag in r.stdout
    assert box.calls() == []


def test_no_colour_codes_when_not_a_terminal(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run")
    assert "\x1b" not in r.stdout + r.stderr


def test_unknown_argument_exits_2_with_usage(box: Box):
    r = box.run("--frobnicate")
    assert r.returncode == 2
    assert "unknown argument: --frobnicate" in r.stderr
    assert "Usage:" in r.stdout


@pytest.mark.parametrize("args", [("--version",), ("--version", "v1;touch x"), ("--version", "$(id)")])
def test_bad_version_exits_2(box: Box, args: tuple[str, ...]):
    assert box.run(*args).returncode == 2


def test_dry_run_prints_the_plan_and_installs_nothing(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "uv tool install --force ws-aitoolkit" in r.stdout
    assert "ws doctor --offline" in r.stdout
    assert "tool install" not in "\n".join(box.calls())


def test_dry_run_with_version_installs_from_the_tag(box: Box):
    box.python()
    box.uv()
    stub(box.bin, "git", "echo git version 0")
    r = box.run("--dry-run", "--version", "v1.2.3")
    assert r.returncode == 0, r.stderr
    assert "git+https://github.com/" in r.stdout
    assert "ws@v1.2.3" in r.stdout


def test_version_needs_git(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run", "--version", "v1.2.3")
    assert r.returncode == 1
    assert "needs git" in r.stderr


def test_pipx_is_used_when_uv_is_missing(box: Box):
    box.python()
    box.pipx()
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "pipx install --force ws-aitoolkit" in r.stdout


def test_no_installer_names_the_exact_install_line_and_troubleshoots(box: Box):
    box.python()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_old_python_without_uv_is_refused_with_an_install_line(box: Box):
    box.python(ok=False)
    box.pipx()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "Python 3.11 or newer is required" in r.stderr


def test_old_python_with_uv_only_warns(box: Box):
    box.python(ok=False)
    box.uv()
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "uv will fetch one" in r.stdout


def test_native_windows_points_at_wsl2(box: Box):
    box.python()
    box.uv()
    (box.bin / "uname").unlink()
    stub(box.bin, "uname", 'case "$1" in -m) echo x86_64;; *) echo MINGW64_NT-10.0;; esac')
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "WSL2" in r.stderr
    assert "wsl --install" in r.stderr


def test_real_run_installs_then_smokes_ws_and_accepts_doctor_exit_1(box: Box):
    box.python()
    box.uv()
    box.ws(doctor_exit=1)
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "uv tool install --force ws-aitoolkit" in box.calls()
    assert "ws --version" in box.calls()
    assert "ws doctor --offline" in box.calls()
    assert "ws --version -> ws 9.9.9" in r.stdout
    assert "ws setup" in r.stdout and "ws doctor" in r.stdout


def test_real_run_fails_when_doctor_does_not_run(box: Box):
    box.python()
    box.uv()
    box.ws(doctor_exit=2)
    r = box.run()
    assert r.returncode == 1
    assert "ws doctor did not run properly (exit 2)" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_no_verify_skips_the_smoke_test(box: Box):
    box.python()
    box.uv()
    box.ws()
    r = box.run("--no-verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("ws ") for c in box.calls())


def test_ws_found_in_the_uv_bin_folder_when_not_on_path(box: Box, tmp_path: Path):
    uv_bin = tmp_path / "uvbin"
    uv_bin.mkdir()
    stub(uv_bin, "ws", 'case "$1" in --version) echo "ws 9.9.9";; doctor) exit 1;; esac')
    box.python()
    box.uv(extra=f'case "$*" in "tool dir --bin") echo "{uv_bin}";; esac')
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not on your PATH yet" in r.stdout


def test_install_failure_prints_troubleshooting(box: Box):
    box.python()
    box.uv(extra='case "$1" in tool) exit 7;; esac')
    r = box.run()
    assert r.returncode == 7
    assert "installation failed (exit 7)" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_script_is_executable_and_has_the_mit_notice():
    assert os.access(INSTALL_SH, os.X_OK)
    head = INSTALL_SH.read_text(encoding="utf-8")[:1200]
    assert "MIT License" in head and "UltraWorkers" in head
