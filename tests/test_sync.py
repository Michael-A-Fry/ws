"""`ws sync`: 4 fetches at a time, each with a time limit; a hung fetch is killed and named, the rest finish."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from ws import cli, sync

# A stand-in for `git fetch`: notes it started (with its pid), hangs in a repo named hang, fails in one named bad.
STUB = """import os, subprocess, sys, time
from pathlib import Path
here = Path.cwd()
log = Path(os.environ["SYNC_LOG"])
with open(log / f"{here.name}.pid", "w") as f:
    f.write(str(os.getpid()))
if here.name == "hang":  # a helper of its own that ignores TERM, as an ssh or https helper might
    helper = subprocess.Popen([sys.executable, "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    with open(log / "helper.pid", "w") as f:
        f.write(str(helper.pid))
    time.sleep(60)
if here.name == "bad":
    sys.stderr.write("fatal: could not read from remote\\n")
    sys.exit(128)
time.sleep(0.2)
with open(log / f"{here.name}.done", "w") as f:
    f.write("ok")
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    root = tmp_path / "code"
    log = tmp_path / "log"
    log.mkdir()
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    monkeypatch.setenv("SYNC_LOG", str(log))
    monkeypatch.setattr(sync, "FETCH", (sys.executable, str(stub)))
    return root, log


def make(root, names):
    for n in names:
        (root / n / ".git").mkdir(parents=True)


def test_a_hung_fetch_is_killed_at_the_timeout_and_the_other_21_finish(box, capsys, monkeypatch):
    root, log = box
    make(root, ["hang", *(f"r{i:02d}" for i in range(21))])
    t0 = time.monotonic()
    code = cli.main(["--json", "sync", "--code-root", str(root), "--timeout-s", "2"])
    took = time.monotonic() - t0
    data = json.loads(capsys.readouterr().out)["data"]["repos"]
    assert code == 1
    assert [r["repo"] for r in data if r["status"] == "timeout"] == ["hang"]
    assert sum(r["status"] == "ok" for r in data) == 21
    assert len(list(log.glob("*.done"))) == 21
    assert took < 30  # not the hung fetch's 60 s
    for name in ("hang.pid", "helper.pid"):  # the fetch and the helper it started, which ignored TERM
        assert gone(int((log / name).read_text())), name


def gone(pid: int, wait_s: float = 5.0) -> bool:
    """Is the process gone (or only a zombie waiting to be reaped)?"""
    end = time.monotonic() + wait_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            state = ""
        if state == "Z":
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.05)


def test_the_text_names_the_hung_and_the_failed_repos(box, capsys):
    root, _ = box
    make(root, ["bad", "hang", "ok"])
    assert cli.main(["sync", "--code-root", str(root), "--timeout-s", "1"]) == 1
    out = capsys.readouterr().out
    assert "hang: timeout (killed after 1 s)" in out
    assert "bad: failed (fatal: could not read from remote)" in out
    assert "fetched 1 of 3" in out


def test_no_more_than_jobs_fetches_run_at_once(box, monkeypatch):
    root, _ = box
    make(root, [f"r{i}" for i in range(10)])
    live, peak = [0], [0]
    lock = threading.Lock()
    real = sync.fetch

    def counted(repo, timeout_s, argv):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            return real(repo, timeout_s, argv)
        finally:
            with lock:
                live[0] -= 1

    monkeypatch.setattr(sync, "fetch", counted)
    got = sync.sync(root, jobs=3, timeout_s=10, argv=sync.FETCH)
    assert len(got) == 10 and all(r["status"] == "ok" for r in got)
    assert peak[0] <= 3


def test_an_interrupt_kills_the_fetches_still_running(box, monkeypatch):
    root, log = box
    make(root, ["hang", "a", "b"])
    real = sync.fetch
    started = threading.Event()

    def interrupted(repo, timeout_s, argv):
        if repo.name == "hang":
            started.set()
            return real(repo, timeout_s, argv)
        started.wait(5)
        deadline = time.monotonic() + 5
        while not (log / "helper.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(sync, "fetch", interrupted)
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        sync.sync(root, jobs=3, timeout_s=60)
    assert time.monotonic() - t0 < 20  # not the hung fetch's 60 s limit
    for name in ("hang.pid", "helper.pid"):
        assert gone(int((log / name).read_text())), name


def test_only_folders_holding_git_are_fetched(box):
    root, _ = box
    make(root, ["a"])
    (root / "plain").mkdir()
    (root / "file.txt").write_text("x")
    (root / "wt").mkdir()
    (root / "wt" / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git is a file
    assert [p.name for p in sync.repos(root)] == ["a", "wt"]


def test_all_clean_exits_0_and_nothing_to_fetch_is_fine(box, capsys):
    root, _ = box
    root.mkdir()
    assert cli.main(["sync", "--code-root", str(root)]) == 0
    make(root, ["a", "b"])
    assert cli.main(["sync", "--code-root", str(root)]) == 0
    assert "fetched 2 of 2" in capsys.readouterr().out


def test_a_missing_code_root_is_named(box, capsys):
    root, _ = box
    assert cli.main(["--json", "sync", "--code-root", str(root / "nope")]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "no_code_root"


@pytest.mark.parametrize("flag", [["--jobs", "0"], ["--timeout-s", "0"], ["--timeout-s", "-1"]])
def test_limits_must_be_above_0(box, flag):
    root, _ = box
    assert cli.main(["sync", "--code-root", str(root), *flag]) == 2
