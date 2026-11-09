"""The holdout: a small black-box set of promises ws makes, checked only through its public entry points (the `ws`
command, as a program, in a scratch home). It always runs, with the rest of the suite. Nothing in ws is imported to
decide a result (the one import spells the hook's `--pin` arguments), and it holds no protected path of its own: the one
it uses is whatever the estate's default policy protects.

The point of keeping it apart is that nothing here is shaped to make a change pass. An edit to this folder needs a
`holdout-change:` line in the pull request body (scripts/test_integrity.py; docs/reference/ci.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ws import paths


@pytest.fixture
def box(tmp_path):
    """A scratch home and a scratch ws folder, and the environment a program started in them gets."""
    home, ws_home = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    ws_home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "WS_HOME": str(ws_home), "LANG": "C.UTF-8"}
    return home, ws_home, env


def ws(env: dict[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ws", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def gate(env: dict[str, str], payload: dict | str) -> subprocess.CompletedProcess[str]:
    """`ws gate check` as a hook runs it: the call on stdin, the folders pinned, exit 0 to allow and 2 to refuse."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return ws(env, "gate", "check", "--runner", "claude-code", *paths.pin_of(env).args(), stdin=raw)


def test_the_gate_denies_a_write_to_a_protected_path(box):
    home, ws_home, env = box
    policy_file = ws_home / "policy.toml"  # ws protects its own policy from the agent's edit tools
    refused = gate(env, {"tool_name": "Write", "tool_input": {"file_path": str(policy_file), "content": "x"}})
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "protected" in refused.stderr
    assert not policy_file.exists()  # and asking didn't write it
    ordinary = {"tool_name": "Write", "tool_input": {"file_path": str(home / "proj" / "notes.txt"), "content": "x"}}
    assert gate(env, ordinary).returncode == 0  # the same call to a place nobody protects goes through


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null", "{}", '{"tool_name": 5, "tool_input": {}}'])
def test_a_payload_the_gate_cannot_read_is_a_deny(box, payload):
    done = gate(box[2], payload)
    assert done.returncode == 2, (payload, done.stdout, done.stderr)
    assert "blocked" in done.stderr


def test_a_policy_that_does_not_parse_is_refused(box):
    _, ws_home, env = box
    (ws_home / "policy.toml").write_text("dial = [\n", encoding="utf-8")
    out = ws_home / "out.json"
    refused = ws(env, "gate", "generate", "--runner", "claude-code", "--out", str(out))
    assert refused.returncode == 1 and "not valid TOML" in refused.stderr, refused.stderr
    assert not out.exists()  # no half-read policy produced a rule file
    # and the gate that depends on it denies rather than guessing
    call = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert gate(env, call).returncode == 2


def test_doctor_offline_runs_and_does_not_crash(box):
    _, _, env = box
    done = ws(env, "doctor", "--offline", "--json")
    assert done.returncode in (0, 1), done.stdout + done.stderr  # 1: this fresh home has things to set up
    assert "Traceback" not in done.stderr
    envelope = json.loads(done.stdout)
    assert envelope["schema"] == "ws.cli/1" and envelope["kind"] == "doctor"
    assert (done.returncode == 0) == (envelope["status"] == "ok")  # the exit code and the status agree
    ids = [c["id"] for c in envelope["data"]["checks"]]
    assert "WS001" in ids and len(ids) == len(set(ids))  # it checked the machine, and no check twice
