"""The session-context guard (ws.guards.session_context): state files, the API key line and the once-a-version news."""

from __future__ import annotations

import json

import pytest

import ws
from ws import gate, policy
from ws.guards import Context
from ws.guards import session_context as sc

KEY = "sk-ant-test-" + "x1y2z3" * 4  # built at run time; a test value, not a real key
CHANGES = """# Changelog

## [Unreleased]

- not yet

## [1.2.3] - 2026-01-01

One line of summary.

### Added

- first change
- second change
- third change
- fourth change
- fifth change

## [1.2.2] - 2025-12-01

- older
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project (a git top level) and a clean environment: no key, an interactive runner, version 1.2.3."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(ws, "__version__", "1.2.3")
    monkeypatch.setattr(sc, "_shipped_changelog", lambda: "")
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    return project


def start(project, cfg=None, session="s1", home=None):
    ctx = Context("claude-code", "start", session, str(project), {}, home=home)
    return sc.judge(None, cfg or sc.config({}, "w"), ctx)


def lines(found):
    return found.reason.splitlines() if found else []


def key_lines(found):
    return [x for x in lines(found) if "ANTHROPIC_API_KEY" in x]


# --- the API key line --------------------------------------------------------------------------------------------


def test_a_key_in_an_interactive_session_is_one_line_and_never_the_key(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    found = start(env)
    assert found.action == "note"
    line = next(x for x in lines(found) if "ANTHROPIC_API_KEY" in x)
    assert "bills that key, not your Claude subscription" in line
    assert KEY not in found.reason and KEY[:8] not in found.reason and KEY[-6:] not in found.reason


@pytest.mark.parametrize("value", [None, "", "  "])
def test_no_key_or_an_empty_one_says_nothing(env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    assert key_lines(start(env)) == []


def test_the_warning_is_on_by_default_and_off_with_warn_api_key_false(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert sc.config({}, "w").warn_api_key is True
    off = sc.config({"warn_api_key": False}, "w")
    assert off.warn_api_key is False
    assert key_lines(start(env, off)) == []


@pytest.mark.parametrize("entry", ["sdk-cli", "sdk-ts", "sdk-py"])
def test_a_headless_runner_session_gets_no_key_line(env, monkeypatch, entry):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entry)
    assert key_lines(start(env)) == []


def test_a_ws_run_job_holds_the_lease_placeholder_and_gets_no_key_line(env, monkeypatch):
    from ws.adapters import claude_code

    assert sc.LEASE_PLACEHOLDER == claude_code.PLACEHOLDER_KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", sc.LEASE_PLACEHOLDER)
    assert key_lines(start(env)) == []


def test_a_desktop_or_terminal_entrypoint_is_interactive(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    for entry in ("cli", "claude-desktop", "claude-vscode"):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entry)
        assert any("ANTHROPIC_API_KEY" in x for x in lines(start(env, session=entry)))


def test_the_key_line_also_shows_with_no_folder(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    found = sc.judge(None, sc.config({}, "w"), Context("claude-code", "start"))
    assert found.action == "note" and KEY not in found.reason


def test_the_key_never_reaches_the_hook_output(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    pol = policy.loads('[guards]\non = ["session-context"]\n')
    raw = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(env)})
    code, out, err = gate.event("claude-code", raw, pol, home=env.parent / "wshome")
    assert code == 0 and "bills that key" in out
    assert KEY not in out + err and KEY[:8] not in out + err


@pytest.mark.parametrize("value", ["yes", 1, "true", []])
def test_warn_api_key_must_be_true_or_false(value):
    with pytest.raises(policy.PolicyError, match="warn_api_key"):
        sc.config({"warn_api_key": value}, "[guards.session-context]")


def test_the_policy_loads_warn_api_key():
    pol = policy.loads('[guards]\non = ["session-context"]\n[guards.session-context]\nwarn_api_key = false\n')
    assert pol.guards.config["session-context"].warn_api_key is False


# --- what changed in ws ------------------------------------------------------------------------------------------


def test_the_first_start_in_a_project_says_the_version_and_the_next_says_nothing(env):
    first = start(env)
    assert lines(first) == ["ws: version 1.2.3"]
    assert start(env) is None
    assert start(env, session="another-session") is None  # once for the project, not for each session


def test_a_new_version_says_it_once_with_the_old_one(env, monkeypatch):
    start(env)
    monkeypatch.setattr(ws, "__version__", "1.3.0")
    assert lines(start(env)) == ["ws: now on 1.3.0 (this project last saw 1.2.3)"]
    assert start(env) is None


def test_each_project_hears_it_once(env):
    other = env.parent / "other"
    (other / ".git").mkdir(parents=True)
    assert start(env) and start(other)
    assert start(env) is None and start(other) is None


def test_a_linked_worktree_shares_its_main_worktrees_record(env):
    gitdir = env / ".git" / "worktrees" / "w"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    linked = env.parent / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")
    assert start(linked)
    assert start(env) is None


def test_the_record_lives_under_the_ws_home_sessions_folder(env):
    start(env)
    (record,) = (env.parent / "wshome" / "sessions").glob("project-*.session-context.json")
    assert json.loads(record.read_text()) == {"version": "1.2.3"}


def test_a_start_with_no_session_id_records_nothing(env):
    ctx = Context("claude-code", "start", "", str(env))
    assert sc.judge(None, sc.config({}, "w"), ctx) is None
    assert not (env.parent / "wshome").exists()


def test_the_shipped_changelog_gives_at_most_four_lines_after_the_version_line(env, monkeypatch):
    monkeypatch.setattr(sc, "_shipped_changelog", lambda: CHANGES)
    got = lines(start(env))
    assert got == ["ws: version 1.2.3", "One line of summary.", "first change", "second change", "third change"]
    assert len(got) <= 5


def test_a_local_version_suffix_still_finds_its_section(env, monkeypatch):
    monkeypatch.setattr(ws, "__version__", "1.2.3+src")
    monkeypatch.setattr(sc, "_shipped_changelog", lambda: CHANGES)
    assert lines(start(env))[:2] == ["ws: version 1.2.3", "One line of summary."]


def test_a_version_the_changelog_lacks_gets_the_version_line_alone(env, monkeypatch):
    monkeypatch.setattr(ws, "__version__", "9.9.9")
    monkeypatch.setattr(sc, "_shipped_changelog", lambda: CHANGES)
    assert lines(start(env)) == ["ws: version 9.9.9"]


def test_changelog_text_is_plain_and_short(env, monkeypatch):
    text = "## [1.2.3]\n\n- " + "word " * 100 + "\n- bell\x07 and\x1b[31m escape\n"
    monkeypatch.setattr(sc, "_shipped_changelog", lambda: text)
    got = lines(start(env))
    assert all(len(x) <= max(sc.CHANGE_WIDTH, 60) for x in got)
    assert not any(c in "\x07\x1b" for x in got for c in x)


def test_changelog_lines_reads_one_section_only():
    assert sc.changelog_lines("1.2.2", CHANGES) == ["older"]
    assert sc.changelog_lines("1.2.3", CHANGES, limit=2) == ["One line of summary.", "first change"]
    assert sc.changelog_lines("1.2", CHANGES) == []
    assert sc.changelog_lines("1.2.3", "") == []


def test_the_shipped_changelog_is_read_only_beside_a_source_tree_or_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "__file__", str(tmp_path / "x" / "ws" / "guards" / "session_context.py"))
    (tmp_path / "x" / "ws" / "guards").mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text(CHANGES)
    assert sc._shipped_changelog() == ""  # a folder up from a package folder, with no pyproject.toml
    (tmp_path / "x" / "ws" / "CHANGELOG.md").write_text(CHANGES)
    assert sc._shipped_changelog().startswith("# Changelog")


def test_an_unwritable_record_loses_the_news_line_but_not_the_rest(env, monkeypatch):
    (env / ".claude-state").mkdir()
    (env / ".claude-state" / "STATE.md").write_text("s")
    (env.parent / "wshome").write_text("a file where the folder should be")
    found = start(env)
    assert lines(found) == [f"ws: .claude-state/STATE.md exists at {env / '.claude-state' / 'STATE.md'}"]


def test_a_record_that_is_not_json_starts_again(env):
    start(env)
    (record,) = (env.parent / "wshome" / "sessions").glob("project-*.session-context.json")
    record.write_text("{not json")
    assert lines(start(env)) == ["ws: version 1.2.3"]
    record.write_text("[1, 2]")
    assert lines(start(env)) == ["ws: version 1.2.3"]


def test_the_lines_come_in_order_files_key_news(env, monkeypatch):
    (env / ".claude-state").mkdir()
    (env / ".claude-state" / "STATE.md").write_text("s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    got = lines(start(env))
    assert "STATE.md exists" in got[0] and "ANTHROPIC_API_KEY" in got[1] and got[2] == "ws: version 1.2.3"
