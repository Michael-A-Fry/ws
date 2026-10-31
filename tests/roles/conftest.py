"""Shared setup for the role adapter tests: a throwaway home, a profile writer and job files. No real systemctl,
launchctl, restic, sops or network anywhere."""

from __future__ import annotations

import pytest

from ws import osinfo
from ws.profile import dumps


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    (home / ".ws").mkdir(mode=0o755)
    return home


@pytest.fixture
def env(box):
    return {"HOME": str(box), "WS_HOME": str(box / ".ws"), "PATH": "/usr/bin:/bin"}


@pytest.fixture
def linux():
    return osinfo.OSInfo("linux", "ubuntu 24.04", "x86_64", "systemd", True)


@pytest.fixture
def mac():
    return osinfo.OSInfo("macos", "26.0", "arm64", "launchd", True)


@pytest.fixture
def write_profile(box):
    def write(data: dict) -> None:
        (box / ".ws" / "profile.toml").write_text(dumps(data), encoding="utf-8")

    return write


@pytest.fixture
def write_job(box):
    def write(name: str, text: str) -> None:
        folder = box / ".ws" / "jobs"
        folder.mkdir(exist_ok=True)
        folder.chmod(0o755)
        f = folder / f"{name}.toml"
        f.write_text(text, encoding="utf-8")
        f.chmod(0o644)

    return write
