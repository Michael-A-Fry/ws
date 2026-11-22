"""The unattended upgrade's acceptance checks and release log: the gate probed through the installed hook, the snapshot they
are compared with, and the CSV that records each outcome. A tmp HOME holds a real `ws install`, so the probes meet the
real gate; the new ws is a stand-in script that answers `--version` and `upgrade --hooks-only`."""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pytest

from ws import cli, upgrade, upgradecheck

STUB_WS = "import os, sys\nprint('ws ' + os.environ.get('STUB_VERSION', '0.1.11.8')) if sys.argv[1:] == ['--version'] else None\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("WS_GATE_DAEMON", "0")
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    assert cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(home / "Notes")]) == 0
    return home


@pytest.fixture
def new_ws(tmp_path):
    script = tmp_path / "stubws.py"
    script.write_text(STUB_WS, encoding="utf-8")
    return [sys.executable, str(script)]


def settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_the_snapshot_counts_the_deny_rules_and_the_probes_the_hook_stops(home):
    snap = upgradecheck.snapshot(os.environ)
    assert snap["deny"] == len(settings(home)["permissions"]["deny"]) > 1
    assert snap["denied"] == len(upgradecheck.deny_probes(os.environ))  # a fresh install stops every one of them


def test_an_installed_ws_passes_every_check(home, new_ws):
    before = upgradecheck.snapshot(os.environ)
    assert upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before) == []


def test_the_new_ws_must_say_the_tag(home, new_ws, monkeypatch):
    monkeypatch.setenv("STUB_VERSION", "9.9.9")
    (problem,) = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, {})
    assert "does not say 0.1.11.8 (it says ws 9.9.9)" in problem


def test_a_hooks_rewrite_that_fails_is_a_problem(home):
    broken = [sys.executable, "-c", "import sys; sys.exit(1 if sys.argv[1:] == ['upgrade', '--hooks-only'] else 0)"]
    assert any("--hooks-only` exits 1" in p for p in upgradecheck.acceptance(os.environ, "v1", broken, {}))


def test_a_deny_rule_fewer_than_before_is_a_problem_and_so_is_a_gate_that_lets_a_probe_through(home, new_ws):
    before = upgradecheck.snapshot(os.environ)
    path = home / ".claude" / "settings.json"
    doc = settings(home)
    doc["permissions"]["deny"].pop()
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert any(
        "deny rules" in p and "fewer than" in p
        for p in upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    )
    doc["hooks"] = {}  # no hook at all: nothing stops anything
    path.write_text(json.dumps(doc), encoding="utf-8")
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    assert any("no gate hook" in p for p in problems) and upgradecheck.snapshot(os.environ)["denied"] == 0


def test_a_gate_that_allows_a_private_key_read_is_caught(home, new_ws, monkeypatch):
    monkeypatch.setattr(upgradecheck, "probe", lambda hook, command, env: 0)  # a hook that lets everything through
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, {})
    assert any("let `cat ~/.ssh/id_rsa` through" in p for p in problems)
    assert any("let `cat ~/.ws/secrets/probe` through" in p for p in problems)


def test_a_session_check_is_run_and_a_failure_is_a_problem(home, new_ws):
    ok = f"{sys.executable} -c 'raise SystemExit(0)'"
    assert upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, {}, session_check=ok) == []
    bad = f"{sys.executable} -c 'raise SystemExit(4)'"
    assert upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, {}, session_check=bad) == [
        "the session check exits 4"
    ]


def test_the_step_marker_the_checks_rely_on_is_the_one_upgrade_sets():
    assert upgrade.STEP_ENV == upgradecheck.STEP_ENV


def test_the_release_log_has_one_header_and_one_row_per_outcome(home):
    upgradecheck.log_row(os.environ, "promote", "0.1.11.6", "v0.1.11.8", 41.6, "ok")
    upgradecheck.log_row(os.environ, "rollback", "0.1.11.8", None, 3, "refused", "no earlier release")
    with open(upgradecheck.log_path(os.environ), newline="", encoding="utf-8") as f:
        raw = list(csv.reader(f))
    assert tuple(raw[0]) == upgradecheck.LOG_COLUMNS and len(raw) == 3
    rows = upgradecheck.last_rows(os.environ)
    assert [(r["verb"], r["from"], r["to"], r["duration_s"], r["result"]) for r in rows] == [
        ("promote", "0.1.11.6", "v0.1.11.8", "42", "ok"),
        ("rollback", "0.1.11.8", "", "3", "refused"),
    ]
    assert rows[1]["reason"] == "no earlier release" and rows[0]["at"].endswith("Z")


def test_an_unwritable_release_log_is_said_and_never_raises(home, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "nowhere" / "x"))
    (home / "nowhere").write_text("a file where a folder should be", encoding="utf-8")
    monkeypatch.setenv("WS_HOME", str(home / "nowhere" / "ws"))
    upgradecheck.log_row(os.environ, "promote", "1", "2", 0, "ok")
    assert "could not write" in capsys.readouterr().err
