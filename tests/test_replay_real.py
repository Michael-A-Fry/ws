"""scripts/replay_real.py: a list of calls through the dispatcher (client to daemon) and in-process, compared with each
other and with an earlier run. Every command here is made up; the script's own rule is that it prints no command."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_real.py"
if not sys.platform.startswith("linux"):
    pytest.skip("the resident gate is replayed on Linux", allow_module_level=True)

spec = importlib.util.spec_from_file_location("replay_real", SCRIPT)
replay = importlib.util.module_from_spec(spec)
sys.modules["replay_real"] = replay
spec.loader.exec_module(replay)

DENIED = "cat ~/.ssh/id_rsa"
FORCE = "git push --force origin main"
CALLS = [
    "ls -la",
    DENIED,
    FORCE,
    "echo planted-marker-4f9c > out.txt",
    {"tool_name": "Read", "tool_input": {"file_path": "~/.aws/credentials"}},
    {"payload": {"tool_name": "Bash", "tool_input": {"command": "git status"}}},
    {"command": "rm -rf build"},
]


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "w"))
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script puts --src in front
    calls = tmp_path / "calls.json"
    calls.write_text(json.dumps(CALLS))
    return tmp_path, calls


def go(calls: Path, *extra: str) -> int:
    return replay.main([str(calls), *extra])


def test_each_shape_of_item_becomes_a_hook_payload():
    plain = replay.payload_of("ls", "/w")
    assert plain == {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "replay", "cwd": "/w"}
    recorded = {"payload": {"tool_name": "Read", "tool_input": {"file_path": "/a"}, "session_id": "s9"}}
    assert replay.payload_of(recorded, "/w")["session_id"] == "s9"  # the recorded id wins
    assert replay.payload_of({"tool_name": "Read", "tool_input": {}}, "/w")["cwd"] == "/w"
    assert replay.payload_of({"command": "ls"}, "/w")["tool_input"] == {"command": "ls"}
    assert replay.payload_of({"nothing": 1}, "/w") is None and replay.payload_of(7, "/w") is None


def test_a_call_is_named_by_a_hash_that_does_not_hold_its_text():
    assert replay.key_of("ls") == replay.key_of("ls") != replay.key_of("ls -l")
    assert len(replay.key_of("ls")) == 16 and replay.key_of({"a": 1, "b": 2}) == replay.key_of({"b": 2, "a": 1})


def test_percentiles():
    assert replay.percentile([5, 1, 3, 2, 4], 50) == 3
    assert replay.percentile([1], 95) == 1


def test_the_two_ways_agree_and_the_same_build_against_its_own_baseline_is_clean(box, capsys):
    tmp, calls = box
    base = tmp / "base.json"
    assert go(calls, "--out", str(base)) == 0
    first = capsys.readouterr().out
    assert "disagree on 0" in first and "calls 7" in first
    results = json.loads(base.read_text())["results"]
    assert set(results.values()) <= {"allow", "deny", "ask"} and "deny" in results.values()
    assert go(calls, "--baseline", str(base)) == 0
    out = capsys.readouterr().out
    assert "newly allowed 0, newly refused 0" in out and "disagree on 0" in out


def test_nothing_in_the_output_or_the_baseline_holds_a_command(box, capsys):
    tmp, calls = box
    base, again = tmp / "base.json", tmp / "again.json"
    go(calls, "--out", str(base))
    go(calls, "--baseline", str(base), "--out", str(again))
    shown = capsys.readouterr().out + base.read_text() + again.read_text()
    for text in ("planted-marker-4f9c", ".ssh", "git push", "rm -rf", "credentials"):
        assert text not in shown, text


def test_a_refusal_the_baseline_did_not_have_is_newly_refused_and_the_run_fails(box, capsys):
    tmp, calls = box
    base = tmp / "base.json"
    assert go(calls, "--out", str(base)) == 0
    data = json.loads(base.read_text())
    denied = replay.key_of(DENIED)
    assert data["results"][denied] == "deny"
    data["results"][denied] = "allow"  # as if the earlier build had let it through
    base.write_text(json.dumps(data))
    capsys.readouterr()
    assert go(calls, "--baseline", str(base)) == 1
    out = capsys.readouterr().out
    assert "newly allowed 0, newly refused 1" in out and "newly refused: call 1" in out


def test_a_call_the_baseline_refused_and_this_build_allows_is_newly_allowed_and_the_run_fails(box, capsys):
    tmp, calls = box
    base = tmp / "base.json"
    assert go(calls, "--out", str(base)) == 0
    data = json.loads(base.read_text())
    data["results"][replay.key_of("ls -la")] = "deny"
    base.write_text(json.dumps(data))
    capsys.readouterr()
    assert go(calls, "--baseline", str(base)) == 1
    assert "newly allowed 1, newly refused 0" in capsys.readouterr().out


def test_a_call_missing_from_the_baseline_fails_the_run(box, capsys):
    tmp, calls = box
    base = tmp / "base.json"
    assert go(calls, "--out", str(base)) == 0
    data = json.loads(base.read_text())
    del data["results"][replay.key_of("ls -la")]
    base.write_text(json.dumps(data))
    capsys.readouterr()
    assert go(calls, "--baseline", str(base)) == 1
    assert "not in the baseline 1" in capsys.readouterr().out


def test_the_dispatcher_and_the_in_process_check_disagreeing_fails_the_run(box, capsys, monkeypatch):
    _, calls = box
    real = replay.Replay.dispatched

    def flipped(self, raw, daemon=True):
        decision, took = real(self, raw, daemon)
        return ("allow" if decision != "allow" else "deny"), took

    monkeypatch.setattr(replay.Replay, "dispatched", flipped)
    assert go(calls) == 1
    # the refusals did come from the daemon, so the run is judged on the decisions, not on a missing daemon
    out = capsys.readouterr().out
    assert "disagree on" in out and "disagree on 0" not in out


def test_the_dispatcher_path_really_reaches_the_daemon(box, capsys):
    _, calls = box
    assert go(calls) == 0
    assert "disagree on 0" in capsys.readouterr().out
    # the script itself fails the run when a refusal did not come from the daemon; this build has one


def test_a_list_with_an_item_that_is_not_a_call_fails_the_run(box, capsys):
    tmp, _ = box
    odd = tmp / "odd.json"
    odd.write_text(json.dumps(["ls", {"nothing": 1}, 5]))
    assert go(odd) == 1
    assert "unreadable 2" in capsys.readouterr().out


def test_a_file_that_is_not_a_list_is_refused(box):
    tmp, _ = box
    bad = tmp / "bad.json"
    bad.write_text('{"a": 1}')
    with pytest.raises(SystemExit):
        go(bad)


def test_a_build_with_no_resident_gate_is_replayed_in_process_only(box, capsys, monkeypatch):
    tmp, calls = box
    real = replay.Replay.__init__

    def without_a_client(self, *a, **k):
        real(self, *a, **k)
        self.client = self.adapters = None

    monkeypatch.setattr(replay.Replay, "__init__", without_a_client)
    base = tmp / "base.json"
    assert go(calls, "--out", str(base)) == 0
    assert "in-process only" in capsys.readouterr().out
    assert len(json.loads(base.read_text())["results"]) == len({replay.key_of(c) for c in CALLS})


def test_timing_prints_p50_and_p95_for_the_daemon_and_the_fallback(box, capsys):
    _, calls = box
    assert go(calls, "--timing", "4") == 0
    out = capsys.readouterr().out
    assert "daemon: 4 calls, p50" in out and "in-process fallback: 4 calls, p50" in out and "p95" in out
