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
    (problem,) = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, upgradecheck.snapshot(os.environ))
    assert "does not say 0.1.11.8 (it says ws 9.9.9)" in problem


def test_a_hooks_rewrite_that_fails_is_a_problem(home):
    broken = [sys.executable, "-c", "import sys; sys.exit(1 if sys.argv[1:] == ['upgrade', '--hooks-only'] else 0)"]
    assert any(
        "--hooks-only` exits 1" in p
        for p in upgradecheck.acceptance(os.environ, "v1", broken, upgradecheck.snapshot(os.environ))
    )


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
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, upgradecheck.snapshot(os.environ))
    assert any("let `cat ~/.ssh/id_rsa` through" in p for p in problems)
    assert any("let `cat ~/.ws/secrets/probe` through" in p for p in problems)


def test_a_session_check_is_run_and_a_failure_is_a_problem(home, new_ws):
    ok = f"{sys.executable} -c 'raise SystemExit(0)'"
    assert (
        upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, upgradecheck.snapshot(os.environ), session_check=ok)
        == []
    )
    bad = f"{sys.executable} -c 'raise SystemExit(4)'"
    assert upgradecheck.acceptance(
        os.environ, "v0.1.11.8", new_ws, upgradecheck.snapshot(os.environ), session_check=bad
    ) == ["the session check exits 4"]


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


# --- the probes themselves: each one is held by a test that fails when it is taken out --------------------------------


def replace_hook(home: Path, command: str) -> None:
    path = home / ".claude" / "settings.json"
    doc = settings(home)
    doc["hooks"] = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_a_gate_that_stops_every_call_is_caught_by_the_allow_probe(home, new_ws):
    """The 22 Nov lockout: the hook refuses `ls -la`. Taking the allow probe out leaves every other check green."""
    before = upgradecheck.snapshot(os.environ)
    replace_hook(home, "echo gate check; exit 2")
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    assert "the claude-code gate stopped `ls -la`" in problems


def test_a_gate_with_no_hook_fails_the_must_deny_count_too_not_only_the_missing_hook(home, new_ws):
    before = upgradecheck.snapshot(os.environ)
    doc = settings(home)
    doc["hooks"] = {}
    (home / ".claude" / "settings.json").write_text(json.dumps(doc), encoding="utf-8")
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    assert any("no gate hook" in p for p in problems)
    assert f"must-deny probes stopped: 0, fewer than the {before['denied']} before the swap" in problems


def test_a_gate_that_stops_fewer_probes_than_before_is_a_weaker_gate_even_if_the_rules_are_the_same(home, new_ws):
    before = upgradecheck.snapshot(os.environ)
    replace_hook(home, "echo gate check; exit 0")  # a hook that lets everything through
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    assert f"must-deny probes stopped: 0, fewer than the {before['denied']} before the swap" in problems
    assert not any("deny rules" in p for p in problems)


@pytest.mark.parametrize("snap", [{}, {"deny": 3}, {"denied": 4}, {"deny": "3", "denied": 4}])
def test_a_snapshot_with_a_count_missing_is_a_problem_not_a_quiet_pass(home, new_ws, snap):
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, snap)
    assert any("has no `" in p and "so the gate was not compared" in p for p in problems)


def test_no_snapshot_at_all_is_said_on_stderr_and_only_the_comparison_is_skipped(home, new_ws, capsys):
    assert upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, None) == []
    assert "not compared with a snapshot" in capsys.readouterr().err


def test_the_probe_time_limit_is_more_than_the_hooks_own_worst_case():
    from ws import gateclient

    assert upgradecheck.PROBE_TIMEOUT_S > gateclient.WORST_CASE_S


def test_a_probe_runs_the_hook_with_no_daemon_even_when_the_environment_asks_for_one(home, tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    monkeypatch.setenv("WS_GATE_DAEMON", "1")
    hook = f'cat >/dev/null; echo "$WS_GATE_DAEMON" > {seen}; exit 0'
    assert upgradecheck.probe(hook, "ls", os.environ) == 0
    assert seen.read_text(encoding="utf-8").strip() == "0"


def test_the_settings_are_read_where_the_runner_reads_them(home, new_ws, tmp_path, monkeypatch):
    """N11: with CLAUDE_CONFIG_DIR set, the hook is in that folder's settings.json and not in ~/.claude."""
    other = tmp_path / "claude-config"
    other.mkdir()
    (other / "settings.json").write_text(
        (home / ".claude" / "settings.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (home / ".claude" / "settings.json").write_text(
        "{}", encoding="utf-8"
    )  # nothing here: the old reading found no hook
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert upgradecheck.settings_path(os.environ) == other / "settings.json"
    assert upgradecheck.hook_command(os.environ) is not None
    assert upgradecheck.snapshot(os.environ)["denied"] == len(upgradecheck.deny_probes(os.environ))


def install_opencode(home: Path) -> None:
    assert cli.main(["install", "--yes", "--runner", "opencode", "--notes", str(home / "Notes")]) == 0


def test_opencode_s_gate_is_probed_too(home, new_ws):
    install_opencode(home)
    argv = upgradecheck.opencode_check(os.environ)
    assert argv and argv[0]
    assert [g.runner for g in upgradecheck.gates(os.environ)] == [
        "opencode"
    ]  # installing for OpenCode moves ws out of Claude's
    assert upgradecheck.snapshot(os.environ)["denied"] == len(upgradecheck.deny_probes(os.environ))
    before = upgradecheck.snapshot(os.environ)
    assert upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before) == []
    plugin = upgradecheck.userconfig.plugin_path(os.environ)
    lines = plugin.read_text(encoding="utf-8").splitlines()
    plugin.write_text(
        "\n".join('const CHECK = ["true"]' if ln.startswith("const CHECK = ") else ln for ln in lines) + "\n",
        encoding="utf-8",
    )
    problems = upgradecheck.acceptance(os.environ, "v0.1.11.8", new_ws, before)
    assert any(
        p.startswith("the opencode gate let `cat ") for p in problems
    )  # a check that always passes is a weak gate
    assert any("must-deny probes stopped" in p for p in problems)


def test_a_log_row_cut_short_is_dropped_so_a_reader_never_meets_none(home):
    upgradecheck.log_row(os.environ, "promote", "0.1.11.6", "v0.1.11.8", 41, "ok")
    with open(upgradecheck.log_path(os.environ), "a", encoding="utf-8") as f:
        f.write("2026-11-23T01:00:00Z,promote,0.1.11.8")  # the write died here
    assert [r["verb"] for r in upgradecheck.last_rows(os.environ)] == ["promote"]
    with open(upgradecheck.log_path(os.environ), "a", encoding="utf-8") as f:
        f.write("\n2026-11-23T02:00:00Z,a,b,c,1,ok,why,extra\n")  # a stray column
    assert len(upgradecheck.last_rows(os.environ)) == 1
    assert all(r["at"] and r["to"] is not None for r in upgradecheck.last_rows(os.environ))
