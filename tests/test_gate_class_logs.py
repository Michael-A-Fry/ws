"""The per-call logs of a session in a data class: the gate's verdict line and record-calls' line go to that class's own
folder (`logs/classes/<name>/`) and to no other, a walled class keeps counts and no payload, and a class folder that is
not the running user's is never written (nor is the shared log used in its place). Every folder, class and command here is
made up under tmp_path; nothing reads the real HOME."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ws import gate, jsonl, paths
from ws.guards import record_calls


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for d in ("home", "wshome", "alpha", "beta", "plain"):
        (tmp_path / d).mkdir()
    return tmp_path


def policy_file(box, guards: str = "") -> str:
    text = (
        f'[classes.alpha]\naccess = "full"\nrepos = ["{box}/alpha"]\n'
        f'[classes.beta]\naccess = "walled"\nos_user = "someone-else"\nrepos = ["{box}/beta"]\n'
        f"{guards}"
    )
    p = box / "wshome" / "policy.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def run(payload: dict, pol: str) -> tuple[int, str, str]:
    argv = ["--runner", "claude-code", "--policy", pol, *paths.pin_of().args()]
    return gate.run_check(gate.hook_args("check", argv), json.dumps(payload))


def call(folder, tool: str = "PlanTool", text: str = "notes") -> dict:
    return {"tool_name": tool, "tool_input": {"notes": text}, "session_id": "s1", "cwd": str(folder)}


def bash(folder, command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "s1", "cwd": str(folder)}


def logs() -> Path:
    return paths.state_dir() / "logs"


def holding(filename: str) -> list[str]:
    """Which logs hold a line, as a path under logs/ (the shared one is the bare file name)."""
    return sorted(str(f.relative_to(logs())) for f in logs().rglob(filename) if jsonl.read(f))


def mine(name: str, filename: str) -> str:
    return f"{paths.CLASS_LOGS}/{name}/{filename}"


def test_a_call_is_logged_in_its_own_class_folder_and_in_no_other(box):
    pol = policy_file(box)
    assert run(call(box / "alpha"), pol)[0] == 0
    assert holding(gate.VERDICT_LOG) == [mine("alpha", gate.VERDICT_LOG)]
    assert run(call(box / "beta"), pol)[0] == 0
    assert holding(gate.VERDICT_LOG) == [mine("alpha", gate.VERDICT_LOG), mine("beta", gate.VERDICT_LOG)]
    (line,) = jsonl.read(logs() / mine("alpha", gate.VERDICT_LOG))
    assert line["tool"] == "PlanTool" and line["decision"] == "allow"


def test_a_folder_in_no_class_is_kept_apart_from_the_named_classes_and_the_shared_log(box):
    pol = policy_file(box)
    assert run(call(box / "plain"), pol)[0] == 0
    assert holding(gate.VERDICT_LOG) == [mine("unclassified", gate.VERDICT_LOG)]


def test_a_policy_with_no_classes_keeps_the_one_shared_log(box):
    pol = box / "wshome" / "policy.toml"
    pol.write_text("[gate]\n", encoding="utf-8")
    assert run(call(box / "alpha"), str(pol))[0] == 0
    assert holding(gate.VERDICT_LOG) == [gate.VERDICT_LOG]


def test_the_class_comes_from_the_real_folder_and_not_from_the_environment(box, monkeypatch):
    pol = policy_file(box)
    link = box / "plain" / "into-beta"
    link.symlink_to(box / "beta", target_is_directory=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box / "alpha"))
    monkeypatch.setenv("PWD", str(box / "alpha"))
    assert run(call(link), pol)[0] == 0
    assert holding(gate.VERDICT_LOG) == [mine("beta", gate.VERDICT_LOG)]


def test_a_class_log_folder_is_private(box):
    pol = policy_file(box)
    assert run(call(box / "alpha"), pol)[0] == 0
    assert (logs() / paths.CLASS_LOGS / "alpha").stat().st_mode & 0o777 == 0o700
    assert (logs() / paths.CLASS_LOGS).stat().st_mode & 0o077 == 0


def test_a_class_folder_that_is_not_the_running_users_is_not_written_and_the_shared_log_is_not_used(box, monkeypatch):
    pol = policy_file(box)
    assert run(call(box / "alpha"), pol)[0] == 0
    before = {f: f.read_bytes() for f in logs().rglob("*.jsonl")}
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # the folder now looks like someone else's
    code, _, err = run(call(box / "alpha", text="again"), pol)
    assert code == 0, err
    assert {f: f.read_bytes() for f in logs().rglob("*.jsonl")} == before  # nothing grew
    assert not (logs() / gate.VERDICT_LOG).exists()  # and no shared file appeared


def test_a_class_folder_that_is_a_link_is_not_written(box):
    pol = policy_file(box)
    elsewhere = box / "elsewhere"
    elsewhere.mkdir()
    (logs() / paths.CLASS_LOGS).mkdir(parents=True)
    (logs() / paths.CLASS_LOGS / "alpha").symlink_to(elsewhere, target_is_directory=True)
    assert run(call(box / "alpha"), pol)[0] == 0
    assert list(elsewhere.iterdir()) == [] and not (logs() / gate.VERDICT_LOG).exists()


# ---- record-calls -----------------------------------------------------------------------------------------------------


RECORD = '[guards]\non = ["record-calls"]\n'


def test_record_calls_writes_the_calls_of_a_class_to_that_class_only(box):
    pol = policy_file(box, RECORD)
    typed = "echo " + "first-marker"
    assert run(bash(box / "alpha", typed), pol)[0] == 0
    assert holding(record_calls.LOG) == [mine("alpha", record_calls.LOG)]
    (line,) = jsonl.read(logs() / mine("alpha", record_calls.LOG))
    assert line["payload"]["tool_input"]["command"] == typed and line["runner"] == "claude-code"


def test_record_calls_keeps_no_payload_for_a_walled_class(box):
    pol = policy_file(box, RECORD)
    typed = "echo " + "second-marker"
    assert run(bash(box / "beta", typed), pol)[0] == 0
    log = logs() / mine("beta", record_calls.LOG)
    (line,) = jsonl.read(log)
    assert "payload" not in line and line["kind"] == "command" and line["runner"] == "claude-code"
    assert "second-marker" not in log.read_text()
    assert holding(record_calls.LOG) == [mine("beta", record_calls.LOG)]


def test_record_calls_keeps_no_payload_for_a_counts_only_class(box):
    text = f'[classes.gamma]\naccess = "counts-only"\nrepos = ["{box}/plain"]\n{RECORD}'
    pol = box / "wshome" / "policy.toml"
    pol.write_text(text, encoding="utf-8")
    assert run(bash(box / "plain", "echo third-marker"), str(pol))[0] == 0
    (line,) = jsonl.read(logs() / mine("gamma", record_calls.LOG))
    assert "payload" not in line and "third-marker" not in json.dumps(line)


def test_record_calls_writes_nothing_where_the_class_folder_is_not_the_running_users(box, monkeypatch):
    pol = policy_file(box, RECORD)
    me = os.geteuid()
    assert run(bash(box / "alpha", "echo one"), pol)[0] == 0
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    assert run(bash(box / "alpha", "echo two"), pol)[0] == 0
    assert len(jsonl.read(logs() / mine("alpha", record_calls.LOG))) == 1
    assert not (logs() / record_calls.LOG).exists()


# ---- readers ----------------------------------------------------------------------------------------------------------


def test_replay_of_the_shared_record_log_also_reads_each_class_copy(box):
    pol = policy_file(box, RECORD)
    assert run(bash(box / "alpha", "echo seen-in-alpha"), pol)[0] == 0
    assert run(bash(box / "beta", "echo seen-in-beta"), pol)[0] == 0  # counts only: no payload to replay
    shared = record_calls.log_path()
    jsonl.append(shared, jsonl.stamped({"runner": "claude-code", "payload": bash(box / "plain", "echo shared")}, 1))
    lines = gate._replay_lines(shared)
    assert len(lines) == 2
    assert "echo shared" in lines[0] and "seen-in-alpha" in lines[1]
    other = box / "other.jsonl"
    other.write_text("{}\n", encoding="utf-8")
    assert gate._replay_lines(other) == ["{}"]


def test_class_log_files_lists_only_what_exists(box):
    assert paths.class_log_files(record_calls.LOG) == []
    assert paths.class_log_path("alpha", record_calls.LOG) is not None
    assert paths.class_log_files(record_calls.LOG) == []  # the folder exists, the file does not


def test_a_call_that_says_it_ran_in_a_full_class_from_a_walled_session_keeps_no_payload(box, monkeypatch):
    pol = policy_file(box, RECORD)
    monkeypatch.chdir(box / "beta")  # where the session started: the runner's folder, not a field of the payload
    assert run(bash(box / "alpha", "echo fourth-marker"), pol)[0] == 0
    log = logs() / mine("beta", record_calls.LOG)
    (line,) = jsonl.read(log)
    assert "payload" not in line and "fourth-marker" not in log.read_text()
    assert holding(record_calls.LOG) == [mine("beta", record_calls.LOG)]  # and nothing in the full class's log
    assert holding(gate.VERDICT_LOG) == [mine("beta", gate.VERDICT_LOG)]


def test_the_project_folder_names_the_start_class_when_the_environment_has_one(box, monkeypatch):
    pol = policy_file(box, RECORD)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box / "beta"))
    assert run(bash(box / "alpha", "echo fifth-marker"), pol)[0] == 0
    assert holding(record_calls.LOG) == [mine("beta", record_calls.LOG)]
    assert "payload" not in jsonl.read(logs() / mine("beta", record_calls.LOG))[0]


def test_a_full_session_keeps_its_payload_when_the_call_stays_in_its_class(box, monkeypatch):
    pol = policy_file(box, RECORD)
    monkeypatch.chdir(box / "alpha")
    assert run(bash(box / "alpha", "echo sixth-marker"), pol)[0] == 0
    (line,) = jsonl.read(logs() / mine("alpha", record_calls.LOG))
    assert line["payload"]["tool_input"]["command"] == "echo sixth-marker"


def test_record_calls_keeps_no_payload_for_a_walled_class_with_no_account(box):
    text = f'[classes.delta]\naccess = "walled"\nrepos = ["{box}/plain"]\n{RECORD}'
    pol = box / "wshome" / "policy.toml"
    pol.write_text(text, encoding="utf-8")
    assert run(bash(box / "plain", "echo seventh-marker"), str(pol))[0] == 0
    (line,) = jsonl.read(logs() / mine("delta", record_calls.LOG))
    assert "payload" not in line and "seventh-marker" not in json.dumps(line)
