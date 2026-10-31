"""ws session: record the release a session started on, nudge exactly once when ws has moved on, never fail the host."""

from __future__ import annotations

import io
import json
import stat
import sys

import pytest

from ws import adapters, cli, session


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for var in ("WS_RELEASE", "WS_RELEASE_FILE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def call(monkeypatch, capsys, sub, sid="s1", stdin=None, *extra) -> tuple[int, str]:
    args = ["session", sub, "--runner", "claude-code", *extra]
    if stdin is None:
        args += ["--session-id", sid]
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(args)
    return code, capsys.readouterr().out


def record(home, sid="s1") -> dict:
    return json.loads((home / "wshome" / "sessions" / f"{sid}.json").read_text())


def test_start_records_the_session_privately(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r1")
    hook = json.dumps({"session_id": "abc-123", "cwd": "/work/here", "hook_event_name": "SessionStart"})
    assert call(monkeypatch, capsys, "start", stdin=hook) == (0, "")
    rec = record(home, "abc-123")
    assert rec["session_id"] == "abc-123"
    assert rec["release"].endswith("+r1")
    assert (rec["runner"], rec["cwd"]) == ("claude-code", "/work/here")
    assert rec["started_at"]
    path = home / "wshome" / "sessions" / "abc-123.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_new_release_nudges_once_then_stays_quiet(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r2026-10-31.1")
    call(monkeypatch, capsys, "start")
    monkeypatch.setenv("WS_RELEASE", "r2026-10-31.2")
    code, out = call(monkeypatch, capsys, "nudge")
    old, new = f"{session.__version__}+r2026-10-31.1", f"{session.__version__}+r2026-10-31.2"
    assert code == 0
    assert out == (
        f"ws {new} is out (this session started on {old}): run checkpoint and restart at the next phase boundary.\n"
    )
    assert record(home)["nudged"] is True
    assert call(monkeypatch, capsys, "nudge") == (0, "")
    monkeypatch.setenv("WS_RELEASE", "r2026-10-31.3")  # nudged once per session, even if ws moves again
    assert call(monkeypatch, capsys, "nudge") == (0, "")


def test_same_release_says_nothing(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r1")
    call(monkeypatch, capsys, "start")
    assert call(monkeypatch, capsys, "nudge") == (0, "")
    assert "nudged" not in record(home)


def test_an_older_release_says_nothing(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r2")
    call(monkeypatch, capsys, "start")
    monkeypatch.setenv("WS_RELEASE", "r1")
    assert call(monkeypatch, capsys, "nudge") == (0, "")


def test_releases_compare_by_number_not_text(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r2026-10-31.9")
    call(monkeypatch, capsys, "start")
    monkeypatch.setenv("WS_RELEASE", "r2026-10-31.10")  # ".10" is after ".9"
    assert "is out" in call(monkeypatch, capsys, "nudge")[1]


def test_a_bumped_package_version_counts_as_newer():
    assert session.is_newer("0.2.0", "0.1.9")
    assert session.is_newer("0.1.0+r2", "0.1.0+r1")
    assert session.is_newer("0.1.0+r1", "0.1.0")
    assert not session.is_newer("0.1.0", "0.1.0")


def test_a_missing_record_is_recorded_now_with_no_nudge(home, monkeypatch, capsys):
    assert call(monkeypatch, capsys, "nudge") == (0, "")
    assert record(home)["release"] == session.current_release()


def test_a_corrupt_record_exits_0_quietly(home, monkeypatch, capsys):
    d = home / "wshome" / "sessions"
    d.mkdir(parents=True)
    (d / "s1.json").write_text("{not json")
    assert call(monkeypatch, capsys, "nudge") == (0, "")
    (d / "s1.json").write_text(json.dumps({"release": 5}))
    assert call(monkeypatch, capsys, "nudge") == (0, "")


def test_start_starts_again_on_a_resumed_session(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r1")
    call(monkeypatch, capsys, "start")
    monkeypatch.setenv("WS_RELEASE", "r2")
    call(monkeypatch, capsys, "nudge")
    call(monkeypatch, capsys, "start")  # the session reloaded its rules: it is on r2 now
    assert "nudged" not in record(home)
    assert call(monkeypatch, capsys, "nudge") == (0, "")


@pytest.mark.parametrize("stdin", ["", "{}", "not json", "[]", '{"session_id": 5}'])
def test_no_usable_input_exits_0_and_writes_nothing(home, monkeypatch, capsys, stdin):
    for sub in ("start", "nudge"):
        assert call(monkeypatch, capsys, sub, stdin=stdin) == (0, "")
    assert not (home / "wshome" / "sessions").exists()


def test_an_unsafe_session_id_stays_inside_the_sessions_folder(home, monkeypatch, capsys):
    call(monkeypatch, capsys, "start", sid="../../evil")
    files = list((home / "wshome" / "sessions").iterdir())
    assert len(files) == 1 and files[0].parent == home / "wshome" / "sessions"
    assert not (home / "evil.json").exists()


def test_an_unwritable_home_still_exits_0(home, monkeypatch, capsys):
    (home / "wshome").write_text("a file where the folder should be")
    assert call(monkeypatch, capsys, "start") == (0, "")
    assert call(monkeypatch, capsys, "nudge") == (0, "")


def test_release_marker_sources(home, monkeypatch):
    v = session.__version__
    assert session.current_release() == v
    (home / "wshome").mkdir()
    (home / "wshome" / "release").write_text("r7\nnotes\n")
    assert session.current_release() == f"{v}+r7"
    folder = home / "rel" / "r9"  # the older layout: a folder named by the id
    folder.mkdir(parents=True)
    monkeypatch.setenv("WS_RELEASE_FILE", str(folder))
    assert session.current_release() == f"{v}+r9"
    monkeypatch.setenv("WS_RELEASE", "r1")  # the variable beats both
    assert session.current_release() == f"{v}+r1"


# --- the generated runner config -----------------------------------------------------------------------------------


def test_claude_settings_run_the_session_hooks_and_stay_idempotent():
    from ws import policy

    mod = adapters.get("claude-code")
    first = mod.generate(policy.default_policy(), ["ws"])
    assert first == mod.generate(policy.default_policy(), ["ws"])
    hooks = json.loads(first["settings.json"])["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "ws session start --runner claude-code || true"
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == "ws session nudge --runner claude-code || true"
    assert "PreToolUse" in hooks


def test_opencode_plugin_wires_the_session_events():
    from ws import policy

    plugin = adapters.get("opencode").generate(policy.default_policy(), ["ws"])["plugins/ws-gate.js"]
    assert "session.created" in plugin and "session.idle" in plugin
    assert 'const WS = ["ws"]' in plugin
