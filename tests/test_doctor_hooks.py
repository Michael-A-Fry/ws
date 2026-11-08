"""WS011: Claude Code is new enough for the gate's hooks to block when they fail (`onFailure`, from 2.1.295)."""

from __future__ import annotations

import pytest

from ws import doctor, run

PROFILE = {"roles": {"runner": {"adapter": "claude-code"}}}


@pytest.fixture
def claude(monkeypatch, tmp_path):
    """`claude` is on PATH and prints the version the test sets."""
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = {"version": "2.1.311"}
    monkeypatch.setattr(run, "exe_version", lambda path, env=None, timeout=10.0: seen["version"])
    return seen, {"PATH": str(exe.parent)}


@pytest.mark.parametrize("version", ["2.1.295", "2.1.311", "2.2.0", "3.0.0", "2.1.311-beta"])
def test_a_claude_code_that_honours_on_failure_is_ok(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = doctor._hooks_check(PROFILE, env)
    assert (check.id, check.status) == ("WS011", "ok") and version in check.detail and not check.fix


@pytest.mark.parametrize("version", ["2.1.294", "2.0.999", "1.9.9", "2.1.9"])
def test_an_older_claude_code_lets_a_failed_hook_through_and_is_a_fail(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = doctor._hooks_check(PROFILE, env)
    assert check.status == "fail" and "2.1.295" in check.detail and check.fix == "update Claude Code"


@pytest.mark.parametrize("version", ["", "nightly", None])
def test_a_version_that_cannot_be_read_is_a_warning(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = doctor._hooks_check(PROFILE, env)
    assert check.status == "warn" and "2.1.295" in check.detail


def test_there_is_no_line_unless_the_runner_is_claude_code_and_it_is_installed(claude, tmp_path):
    _, env = claude
    assert doctor._hooks_check({"roles": {"runner": {"adapter": "opencode"}}}, env) == []
    assert doctor._hooks_check({"roles": {}}, env) == []
    assert doctor._hooks_check({}, env) == []
    assert doctor._hooks_check(PROFILE, {"PATH": str(tmp_path / "nowhere")}) == []  # WS101 reports a missing runner


def test_the_line_is_in_the_doctor_and_in_the_reference(claude, tmp_path):
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "docs" / "reference" / "doctor-checks.md").read_text(encoding="utf-8")
    assert "| WS011 | hooks |" in page
    assert doctor._version_tuple("2.1.311 (Claude Code)") == (2, 1, 311) and doctor._version_tuple("x") is None
