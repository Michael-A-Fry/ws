"""`ws node`: a footprint ledger written as each change is made, and a remove that undoes it, newest first.

The tests set HOME and WS_HOME to a scratch folder. The systemd timer is switched on and off through an injected runner,
so no test calls `systemctl`. The container test uses a local image and a container runtime if the machine has them, and
never pulls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ws import cli, jsonl, node, output

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "wshome"
    home.mkdir()
    monkeypatch.setenv("WS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "WS_GATED_SESSION", "WS_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    return home


def ledger_lines(home: Path) -> list[dict]:
    return jsonl.read(home / "node" / "footprint.jsonl")


def tree(root: Path) -> dict[str, str]:
    """Every path under root with its kind and, for a file, the hash of its content."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_ledger_written_before_each_change(box, monkeypatch):
    seen: list[tuple[str, str, list[str]]] = []
    for kind in ("dir", "file"):
        real = node._DO[kind]

        def spy(step, real=real, kind=kind):
            lines = [(r["op"], r["step"]) for r in ledger_lines(box)]
            seen.append((kind, step.target, [f"{op}:{sid}" for op, sid in lines]))
            real(step)

        monkeypatch.setitem(node._DO, kind, spy)
    steps = node.join()
    assert steps and len(seen) == len(steps)
    for i, (kind, target, before) in enumerate(seen):
        assert f"intent:s{i + 1}" in before, f"the intent for {target} was not in the ledger when ws made the change"
        assert f"done:s{i + 1}" not in before  # and its done is not written until the change is made
        assert before == [x for n in range(1, i + 1) for x in (f"intent:s{n}", f"done:s{n}")] + [f"intent:s{i + 1}"]
    lines = ledger_lines(box)
    assert [(r["op"], r["step"]) for r in lines] == [
        (op, f"s{n}") for n in range(1, len(steps) + 1) for op in ("intent", "done")
    ]
    for r in lines:
        jsonl.check_stamp(r)
        assert r["type"] == "ws.footprint/1" and r["schema"] == 1 and isinstance(r["schema"], int)
        assert r["node"] == lines[0]["node"]
    assert {r["kind"] for r in lines if r["op"] == "intent"} == {"dir", "file"}


def test_a_change_that_fails_leaves_its_intent_and_remove_copes(box, monkeypatch):
    real = node._DO["file"]
    count = [0]

    def break_on_second(step):
        count[0] += 1
        if count[0] == 2:
            raise OSError(28, "No space left on device")
        real(step)

    monkeypatch.setitem(node._DO, "file", break_on_second)
    with pytest.raises(OSError):
        node.join()
    ops = [(r["op"], r["step"]) for r in ledger_lines(box)]
    assert ops[-1][0] == "intent" and ("done", ops[-1][1]) not in ops  # an intent with no done
    monkeypatch.setitem(node._DO, "file", real)
    result = node.remove()
    assert not result.left  # the half-made step is understood: nothing of it exists, so there is nothing to undo
    assert not (box / "node").exists() and not (box / "queue").exists() and not (box / "jobs").exists()


def test_an_intent_with_no_done_whose_file_was_written_is_removed(box, monkeypatch):
    """The crash came after the file was written and before its done line."""
    real = node._DO["file"]
    state = {"n": 0}

    def write_then_die(step):
        real(step)
        state["n"] += 1
        if state["n"] == 1:
            raise KeyboardInterrupt

    monkeypatch.setitem(node._DO, "file", write_then_die)
    with pytest.raises(KeyboardInterrupt):
        node.join()
    assert (box / "node" / "node.json").is_file()
    monkeypatch.setitem(node._DO, "file", real)
    assert not node.remove().left
    assert not (box / "node").exists()


def test_remove_undoes_in_reverse(box, monkeypatch):
    steps = node.join(timer=True, runner=lambda argv: (0, ""))
    order = [s.id for s in steps]
    undone: list[str] = []
    for kind in ("dir", "file"):
        real = node._UNDO[kind]

        def spy(step, real=real):
            undone.append(step.id)
            return real(step)

        monkeypatch.setitem(node._UNDO, kind, spy)
    runs: list[list[str]] = []
    result = node.remove(runner=lambda argv: runs.append(argv) or (0, ""))
    assert not result.left
    assert [s.id for s in result.removed][:1] == [order[-1]]  # the timer, made last, goes first
    assert [i for i in undone] == [i for i in reversed(order) if i in undone]  # the rest in reverse order
    assert any("disable" in a for a in runs[0]) and any(a[:3] == ["systemctl", "--user", "daemon-reload"] for a in runs)
    # nothing is left: the ledger and everything ws made are gone
    assert not (box / "node").exists() and not (box / "queue").exists() and not (box / "jobs").exists()
    again = node.remove()  # and a second remove has nothing to do, and says nothing is wrong
    assert not again.left and not again.removed


def test_remove_lists_what_is_left_on_partial_failure(box, monkeypatch, capsys):
    steps = node.join()
    drain = next(s for s in steps if s.target.endswith("queue-drain.toml"))
    real = node._UNDO["file"]

    def refuse_one(step):
        if step.id == drain.id:
            raise PermissionError(13, "Permission denied")
        return real(step)

    monkeypatch.setitem(node._UNDO, "file", refuse_one)
    code = cli.main(["node", "remove"])
    out = capsys.readouterr().out
    assert code == 1 and drain.target in out and "left" in out
    assert (box / "node" / "footprint.jsonl").is_file()  # the ledger stays: it is what the next remove works from
    assert Path(drain.target).is_file() and not (box / "queue").exists()  # the rest was undone
    code = cli.main(["node", "remove", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert code == 1 and env["status"] == output.ATTENTION and env["kind"] == "node.remove"
    # the file, and the folder that cannot go while it is in it
    assert [x["target"] for x in env["data"]["left"]] == [drain.target, str(box / "jobs")]
    monkeypatch.setitem(node._UNDO, "file", real)  # the cause is mended
    assert cli.main(["node", "remove"]) == 0
    capsys.readouterr()
    assert not Path(drain.target).exists() and not (box / "node").exists() and not (box / "jobs").exists()


def test_changed_file_is_left_and_named(box, capsys):
    steps = node.join()
    drain = Path(next(s for s in steps if s.target.endswith("queue-drain.toml")).target)
    mine = drain.read_text(encoding="utf-8") + "# I changed this\n"
    drain.write_text(mine, encoding="utf-8")
    code = cli.main(["node", "remove"])
    out = capsys.readouterr().out
    assert code == 1 and str(drain) in out and "changed since ws wrote it" in out
    assert drain.read_text(encoding="utf-8") == mine  # your file is still yours
    assert (box / "node" / "footprint.jsonl").is_file() and (box / "jobs").is_dir()  # and what holds it stays
    # a file that was replaced by a link, or is a folder now, is not deleted either
    drain.unlink()
    drain.mkdir()
    assert node.remove().left and drain.is_dir()


def test_a_file_of_yours_with_the_same_name_is_neither_overwritten_nor_claimed(box):
    (box / "jobs").mkdir()
    os.chmod(box / "jobs", 0o700)
    mine = box / "jobs" / "queue-drain.toml"
    mine.write_text('command = "queue"\nschedule = "every 20m"\n', encoding="utf-8")
    steps = node.join()
    assert not any(s.target == str(mine) for s in steps)
    assert not node.remove().left
    assert mine.read_text(encoding="utf-8").endswith('"every 20m"\n') and (box / "jobs").is_dir()  # still there


def test_install_uninstall_diff_to_zero(box):
    """On a scratch machine: the whole tree before a join and after its remove is the same."""
    (box / "profile.toml").write_text("[machine]\nos = 'linux'\n", encoding="utf-8")
    (box / "logs").mkdir()
    before = tree(box)
    node.join(every=15, timer=True, runner=lambda argv: (0, ""))
    assert tree(box) != before and (box / "queue").is_dir()
    assert (box / "jobs" / "queue-drain.toml").read_text(encoding="utf-8").endswith('schedule = "every 15m"\n')
    result = node.remove(runner=lambda argv: (0, ""))
    assert not result.left
    assert tree(box) == before


def test_join_is_idempotent_and_show_reads_the_ledger(box, capsys):
    assert cli.main(["node", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["steps"] == []
    first = node.join()
    second = node.join()  # already joined: left as it is
    assert [s.id for s in second] == [s.id for s in first] and len(ledger_lines(box)) == 2 * len(first)
    assert cli.main(["node", "show", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "node.show" and {s["state"] for s in env["data"]["steps"]} == {"done"}
    assert cli.main(["node", "join", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "node.join" and env["status"] == "ok"
    assert cli.main(["node", "remove", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["left"] == []
    assert cli.main(["node", "join", "--every", "4"]) == 2  # outside 5 to 30
    capsys.readouterr()


# ---- the same round trip in a container ------------------------------------------------------------------------

SNAPSHOT = (
    "import hashlib, os, sys\n"
    "root = sys.argv[1]\n"
    "for d, dirs, files in sorted(os.walk(root)):\n"
    "    dirs.sort()\n"
    "    for n in sorted(dirs + files):\n"
    "        p = os.path.join(d, n)\n"
    "        h = '' if os.path.isdir(p) else hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
    "        print(os.path.relpath(p, root), 'dir' if os.path.isdir(p) else 'file', oct(os.stat(p).st_mode & 0o777), h)\n"
)

# Run with HOME at an empty folder: join, then remove, and compare the folder before and after.
ROUND_TRIP = """
set -eu
mkdir -p "$HOME" "$SNAPS"
$PY -c "$SNAPSHOT" "$HOME" > "$SNAPS/before"
$PY -m ws node join --every 10
$PY -m ws node show > /dev/null
$PY -m ws node remove
$PY -c "$SNAPSHOT" "$HOME" > "$SNAPS/after"
diff "$SNAPS/before" "$SNAPS/after"
echo diff-is-zero
"""


def round_trip_env(home: Path, snaps: Path, py: str, src: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SNAPS": str(snaps),
        "PY": py,
        "PYTHONPATH": src,
        "SNAPSHOT": SNAPSHOT,
        "LANG": "C.UTF-8",
    }


def test_the_round_trip_script_leaves_a_fresh_home_as_it_found_it(tmp_path):
    """The script the container runs, run here on a fresh home (no XDG variables): the folders ws makes are all taken away."""
    env = round_trip_env(tmp_path / "freshhome", tmp_path / "snaps", sys.executable, str(ROOT / "src"))
    done = subprocess.run(["sh", "-c", ROUND_TRIP], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().endswith("diff-is-zero")
    assert (tmp_path / "snaps" / "before").read_text() == (tmp_path / "snaps" / "after").read_text() == ""


def _container() -> tuple[str, str] | None:
    runtime = shutil.which("docker") or shutil.which("podman")
    if not runtime:
        return None
    image = os.environ.get("WS_TEST_NODE_IMAGE", "python:3.12-slim")
    try:
        have = subprocess.run([runtime, "image", "inspect", image], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (runtime, image) if have.returncode == 0 else None  # a local image only: this never pulls


def test_install_uninstall_diff_to_zero_in_a_container(tmp_path):
    """In a container when docker or podman and a local image are there; else on the host, in a home that already holds a file."""
    found = _container()
    if found is None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".profile").write_text("export A=1\n")  # something already there must come through untouched
        env = round_trip_env(home, tmp_path / "snaps", sys.executable, str(ROOT / "src"))
        done = subprocess.run(
            ["sh", "-c", ROUND_TRIP], env=env, capture_output=True, text=True, timeout=120, check=False
        )
        assert (home / ".profile").read_text() == "export A=1\n"
    else:
        runtime, image = found
        argv = [
            runtime, "run", "--rm", "--network", "none", "-v", f"{ROOT / 'src'}:/ws-src:ro",
            "-e", "HOME=/tmp/h", "-e", "SNAPS=/tmp/snaps", "-e", "PY=python3", "-e", "PYTHONPATH=/ws-src",
            "-e", f"SNAPSHOT={SNAPSHOT}", image, "sh", "-c", ROUND_TRIP,
        ]  # fmt: skip
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().endswith("diff-is-zero")
