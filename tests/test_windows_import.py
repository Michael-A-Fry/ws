"""Every module under src/ws imports on native Windows, where `ws doctor` must still run and point at WSL2.

Native Windows has no `fcntl`, `termios`, `pwd`, `grp`, `pty` or `resource`, and `os` lacks `O_DIRECTORY`, `O_NOFOLLOW`,
`O_NOCTTY`, `O_NONBLOCK`, `getuid`, `geteuid`, `fchmod`, `fchown`, `getpgid`, `setsid`, `killpg`, `mkfifo` and the `sched_*`
calls. A module that uses one at import time (a constant such as `os.O_DIRECTORY | os.O_NOFOLLOW` at the top of the
file, a top-level `import fcntl`) kills every `ws` command there. This class has reached CI three times, so it is checked
here, on any OS: a subprocess takes those names away, blocks those imports with a meta-path finder, says it is `win32`,
and imports every module in the package. Use the name inside the function, or `getattr(os, "O_NOFOLLOW", 0)`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

SCRIPT = textwrap.dedent(
    """
    import ast, importlib, os, pathlib, pkgutil, sys

    # The standard library itself needs the real platform to load (shutil imports _winapi once it thinks it is Windows),
    # so load what ws uses first; only then become "Windows".
    for path in pathlib.Path(sys.argv[1], "ws").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            )
            for name in names:
                if name.split(".")[0] in sys.stdlib_module_names:
                    try:
                        importlib.import_module(name)
                    except ImportError:
                        pass
    for extra in ("importlib.metadata", "zipfile", "encodings.idna", "email.message", "ssl", "urllib.request", "http.client", "http.server"):
        importlib.import_module(extra)

    for name in (
        "O_DIRECTORY", "O_NOFOLLOW", "O_NOCTTY", "O_NONBLOCK", "O_CLOEXEC", "getuid", "geteuid", "getgid", "fchmod",
        "fchown", "getpgid", "getpgrp", "setsid", "killpg", "mkfifo", "sched_getaffinity", "sched_setaffinity",
        "sched_yield", "fork", "forkpty", "wait3", "wait4", "getlogin_r",
    ):
        if hasattr(os, name):
            delattr(os, name)
    import ssl

    ssl.enum_certificates = ssl.enum_crls = lambda store: []  # what real Windows has; the fake platform makes ssl ask
    BLOCKED = {"fcntl", "termios", "pwd", "grp", "pty", "resource", "tty", "syslog"}

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"No module named {fullname!r} (blocked: native Windows)")
            return None

    for mod in BLOCKED:
        sys.modules.pop(mod, None)
    sys.meta_path.insert(0, Finder())
    sys.platform = "win32"

    sys.path.insert(0, sys.argv[1])
    import ws

    failed = []
    for info in pkgutil.walk_packages(ws.__path__, "ws.", onerror=lambda n: failed.append(f"{n}: a package that did not import")):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            import traceback
            failed.append(f"{info.name}: {type(e).__name__}: {e}\\n" + "".join(traceback.format_exc().splitlines(True)[-6:]))
    print("\\n".join(failed))
    sys.exit(1 if failed else 0)
    """
)


def test_every_module_imports_on_native_windows_without_posix_only_names():
    proc = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT, str(SRC)], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, f"modules that die at import on native Windows:\n{proc.stdout}{proc.stderr}"
