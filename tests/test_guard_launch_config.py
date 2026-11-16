"""The guard-launch and config-guard guards (ws.guards.guard_launch, ws.guards.config_guard), each through the real
hook entry: what they refuse, what they let through, and that a guard which raises or whose process is killed denies.
Repositories are planted folders (a `.git` folder is a main checkout, a `.git` file a linked worktree)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from ws import approvals, gate, guards, paths, policy
from ws.adapters import claude_code


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    (tmp_path / "main" / ".git").mkdir(parents=True)
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "plain").mkdir()
    (tmp_path / "owner").mkdir()
    (tmp_path / "guarded").mkdir()
    return tmp_path


def payload(tool, cwd, **tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input, "session_id": "s1", "cwd": str(cwd)})


def run(box, text, body, *extra):
    file = box / "w.toml"
    file.write_text(text)
    argv = ["--runner", "claude-code", *paths.pin_of().args(), "--policy", str(file), *extra]
    return gate.run_hook("check", argv, body)[0]


LAUNCH = '[guards]\non = ["guard-launch"]\n'


def config_policy(box, extra=""):
    return (
        f'[guards]\non = ["config-guard"]\n[guards.config-guard]\npaths = ["{box / "guarded"}"]\n'
        f'owners = ["{box / "owner"}"]\n{extra}'
    )


@pytest.mark.parametrize(
    "tool, inp, where, want",
    [
        ("Workflow", {"name": "x"}, "main", 0),
        ("Workflow", {"name": "x"}, "wt", 2),
        ("Workflow", {"name": "x"}, "plain", 2),
        ("Agent", {"prompt": "p", "isolation": "worktree"}, "wt", 2),
        ("Agent", {"prompt": "p", "isolation": "worktree"}, "main", 0),
        ("Agent", {"prompt": "p"}, "wt", 0),  # not isolated: not this guard's business
        ("Task", {"prompt": "p", "isolation": "worktree"}, "plain", 2),
    ],
)
def test_guard_launch_only_from_a_main_checkout(box, tool, inp, where, want):
    assert run(box, LAUNCH, payload(tool, box / where, **inp)) == want


def test_guard_launch_refuses_a_main_checkout_kept_under_a_worktrees_folder(box):
    inner = box / "main" / ".claude" / "worktrees" / "x"
    (inner / ".git").mkdir(parents=True)
    assert run(box, LAUNCH, payload("Workflow", inner, name="x")) == 2


def test_config_guard_edits_only_by_the_owner(box):
    target = box / "guarded" / "settings.json"
    other = box / "plain" / "notes.txt"
    text = config_policy(box)
    assert run(box, text, payload("Edit", box / "plain", file_path=str(target), old_string="a", new_string="b")) == 2
    assert run(box, text, payload("Write", box / "owner", file_path=str(target), content="x")) == 0
    assert run(box, text, payload("Write", box / "plain", file_path=str(other), content="x")) == 0
    link = box / "plain" / "link"
    link.symlink_to(target)  # a path through a link is the file it points at
    assert run(box, text, payload("Write", box / "plain", file_path=str(link), content="x")) == 2
    assert (
        run(
            box,
            '[guards]\non = ["config-guard"]\n',
            payload("Write", box / "plain", file_path=str(target), content="x"),
        )
        == 0
    )


@pytest.mark.parametrize(
    "name, body",
    [
        ("guard-launch", lambda b: payload("Workflow", b / "main", name="x")),
        ("config-guard", lambda b: payload("Write", b / "owner", file_path=str(b / "plain" / "f"), content="x")),
    ],
)
def test_a_guard_that_raises_denies_the_call(box, monkeypatch, name, body):
    text = config_policy(box) if name == "config-guard" else LAUNCH
    assert run(box, text, body(box)) == 0  # the call is fine until the guard breaks

    def boom(*a, **k):
        raise RuntimeError("planted")

    monkeypatch.setattr(guards.get(name), "judge", boom)
    assert run(box, text, body(box)) == 2


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX shell")
@pytest.mark.parametrize("name", ["guard-launch", "config-guard"])
def test_a_guard_process_killed_with_sigkill_denies_the_call(box, monkeypatch, name):
    killer = box / "killer"
    killer.write_text("#!/bin/sh\nkill -9 $$\n")
    killer.chmod(killer.stat().st_mode | stat.S_IXUSR)
    pol = policy.loads(config_policy(box) if name == "config-guard" else LAUNCH)
    conf = json.loads(claude_code.generate(pol, [str(killer)], box / "out", box / "w.toml")["settings.json"])
    command = conf["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = {**os.environ, "HOME": str(box / "home"), "WS_GATE_DAEMON": "0"}
    done = subprocess.run(
        ["sh", "-c", command],
        input=payload("Workflow", box / "main", name="x"),
        text=True,
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 2, done.stderr


def test_a_guard_in_log_mode_lets_the_call_go_where_the_wall_is_proven(box, monkeypatch):
    monkeypatch.setattr(gate, "_platform", lambda: "linux")
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/opt/fake/bwrap" if n == "bwrap" else real(n, *a, **k))
    monkeypatch.setenv(approvals.SANDBOX_MARKER, "1")
    body = payload("Workflow", box / "plain", name="x")
    assert run(box, LAUNCH.replace("]\n", "]\n", 1) + '[guards.guard-launch]\nmode = "log"\n', body) == 0
    assert run(box, LAUNCH, body) == 2  # the default is enforce
