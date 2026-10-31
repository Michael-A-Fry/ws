"""Which machine are we on: macOS, Linux, Windows via WSL2, or native Windows (not supported; use WSL2)."""

from __future__ import annotations

import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path

SUPPORTED = ("linux", "macos", "wsl")


@dataclass(frozen=True)
class OSInfo:
    kind: str  # linux | macos | wsl | windows | other
    release: str  # distro or OS version, e.g. "ubuntu 24.04", "26.0"
    arch: str
    scheduler: str  # the native job runner ws will use: systemd | launchd | none
    supported: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _os_release(root: Path) -> str:
    fields = {}
    for line in _read(root / "etc/os-release").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value.strip().strip('"')
    return " ".join(v for v in (fields.get("ID", ""), fields.get("VERSION_ID", "")) if v) or "unknown"


def _is_wsl(env: dict, root: Path) -> bool:
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    return "microsoft" in _read(root / "proc/sys/kernel/osrelease").lower()


def detect(system: str | None = None, env: dict | None = None, root: Path = Path("/")) -> OSInfo:
    """Detect the OS. `system`, `env` and `root` exist so tests can stand in for another machine."""
    system = system or platform.system()
    env = os.environ if env is None else env
    arch = platform.machine().lower() or "unknown"
    if system == "Darwin":
        kind, release, scheduler = "macos", platform.mac_ver()[0] or "unknown", "launchd"
    elif system == "Linux":
        kind = "wsl" if _is_wsl(env, root) else "linux"
        release = _os_release(root)
        scheduler = "systemd" if (root / "run/systemd/system").is_dir() else "none"
    elif system == "Windows":
        kind, release, scheduler = "windows", platform.release() or "unknown", "none"
    else:
        kind, release, scheduler = "other", system or "unknown", "none"
    return OSInfo(kind=kind, release=release, arch=arch, scheduler=scheduler, supported=kind in SUPPORTED)
