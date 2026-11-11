"""`ws status --json` reports the sandbox and the permission mode in force, read from the settings files: `.data.sandbox` and
`.data.permission_mode`. Files are read without following a link and capped; one that does not read is named, never a
crash. Everything runs under a tmp HOME and WS_HOME; the managed-settings files are pointed at a tmp folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import cli, status, wallsettings


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    managed = tmp_path / "managed"
    monkeypatch.setattr(wallsettings, "MANAGED_FILES", (str(managed / "managed-settings.json"),))
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(managed / "managed-settings.d"),))
    repo = tmp_path / "work" / "app"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    return tmp_path


def put(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def user_file(box) -> Path:
    return box / "home" / ".claude" / "settings.json"


def generated_file(box) -> Path:
    return box / "home" / ".ws" / "gate" / "claude-code" / "settings.json"


def data_of(capsys) -> dict:
    assert cli.main(["status", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["data"]


WS_ENV = {"env": {"WS_SANDBOXED": "1"}}
BLOCK = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}


# --- sandbox ----------------------------------------------------------------------------------------------------------


def test_the_json_shape_with_no_settings_is_all_null(box, capsys):
    data = data_of(capsys)
    assert data["sandbox"] == {
        "enabled": None,
        "allowUnsandboxedCommands": None,
        "failIfUnavailable": None,
        "source": None,
    }
    assert data["permission_mode"] == {"mode": None, "source": None}


def test_the_sandbox_is_read_from_the_user_file_that_carries_ws_entries(box, capsys):
    f = put(user_file(box), {**WS_ENV, "sandbox": BLOCK})
    assert data_of(capsys)["sandbox"] == {
        "enabled": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "source": str(f),
    }


def test_a_user_file_without_ws_entries_is_not_the_source_and_the_generated_file_is(box, capsys):
    put(user_file(box), {"sandbox": {"enabled": False}})  # the person's own: not a statement about ws's wall
    gen = put(generated_file(box), {**WS_ENV, "sandbox": BLOCK})
    sb = data_of(capsys)["sandbox"]
    assert sb["source"] == str(gen) and sb["enabled"] is True


def test_the_generated_file_is_read_when_ws_has_not_merged_into_the_user_file(box, capsys):
    gen = put(generated_file(box), {"sandbox": {"enabled": True, "allowUnsandboxedCommands": True}})
    sb = data_of(capsys)["sandbox"]
    assert sb == {"enabled": True, "allowUnsandboxedCommands": True, "failIfUnavailable": None, "source": str(gen)}


def test_ws_entries_with_no_sandbox_block_say_the_sandbox_is_off(box, capsys):
    f = put(user_file(box), {"env": {"WS_SANDBOXED": "0"}})
    sb = data_of(capsys)["sandbox"]
    assert sb["enabled"] is False and sb["allowUnsandboxedCommands"] is None and sb["source"] == str(f)


def test_a_value_that_is_not_a_boolean_is_null(box, capsys):
    put(user_file(box), {**WS_ENV, "sandbox": {"enabled": "yes", "failIfUnavailable": 1}})
    sb = data_of(capsys)["sandbox"]
    assert sb["enabled"] is None and sb["failIfUnavailable"] is None


def test_an_unreadable_user_file_is_named_and_the_generated_file_is_still_used(box, capsys):
    bad = put(user_file(box), "{ not json")
    gen = put(generated_file(box), {**WS_ENV, "sandbox": BLOCK})
    sb = data_of(capsys)["sandbox"]
    assert sb["source"] == str(gen) and sb["enabled"] is True
    assert len(sb["unreadable"]) == 1 and sb["unreadable"][0].startswith(f"{bad}: ")


def test_a_settings_file_that_is_a_link_is_not_followed(box, capsys):
    real = put(box / "elsewhere.json", {**WS_ENV, "sandbox": BLOCK})
    user_file(box).parent.mkdir(parents=True)
    user_file(box).symlink_to(real)
    sb = data_of(capsys)["sandbox"]
    assert sb["enabled"] is None and sb["source"] is None  # nothing was read through the link
    assert sb["unreadable"] and str(user_file(box)) in sb["unreadable"][0]


def test_a_settings_file_over_the_cap_is_unreadable(box, capsys, monkeypatch):
    monkeypatch.setattr(status, "MAX_SETTINGS_BYTES", 64)
    put(user_file(box), {**WS_ENV, "sandbox": BLOCK, "pad": "x" * 200})
    sb = data_of(capsys)["sandbox"]
    assert sb["enabled"] is None and "too large" in sb["unreadable"][0]


def test_a_settings_file_that_is_not_an_object_is_unreadable(box, capsys):
    put(user_file(box), "[1, 2]")
    assert "not a JSON object" in data_of(capsys)["sandbox"]["unreadable"][0]


# --- permission mode --------------------------------------------------------------------------------------------------


def test_the_mode_is_the_default_mode_of_the_user_file(box, capsys):
    f = put(user_file(box), {"permissions": {"defaultMode": "acceptEdits"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "acceptEdits", "source": str(f)}


def test_a_higher_precedence_file_wins(box, capsys):
    put(user_file(box), {"permissions": {"defaultMode": "acceptEdits"}})
    proj = put(box / "work" / "app" / ".claude" / "settings.json", {"permissions": {"defaultMode": "plan"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "plan", "source": str(proj)}
    local = put(box / "work" / "app" / ".claude" / "settings.local.json", {"permissions": {"defaultMode": "default"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "default", "source": str(local)}
    managed = put(box / "managed" / "managed-settings.d" / "10-x.json", {"permissions": {"defaultMode": "plan"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "plan", "source": str(managed)}


def test_bypass_from_a_project_file_is_skipped_because_claude_code_ignores_it_there(box, capsys):
    f = put(user_file(box), {"permissions": {"defaultMode": "acceptEdits"}})
    put(box / "work" / "app" / ".claude" / "settings.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "acceptEdits", "source": str(f)}


def test_bypass_in_the_user_file_is_reported_and_flagged_in_the_text(box, capsys):
    f = put(user_file(box), {"permissions": {"defaultMode": "bypassPermissions"}})
    assert data_of(capsys)["permission_mode"] == {"mode": "bypassPermissions", "source": str(f)}
    assert cli.main(["status", "--no-color"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("permission mode:"))
    assert "bypassPermissions" in line and str(f) in line and "nothing asks" in line


def test_an_unreadable_settings_file_is_named_under_the_permission_mode(box, capsys):
    bad = put(box / "work" / "app" / ".claude" / "settings.json", "{")
    pm = data_of(capsys)["permission_mode"]
    assert pm["mode"] is None and pm["unreadable"][0].startswith(f"{bad}: ")


def test_ws_never_writes_a_default_mode_into_what_it_generates(box):
    from ws import policy
    from ws.adapters import claude_code

    for scope in (False, True):
        pol = policy.loads("")
        files = claude_code.generate(pol, ["ws"], box / "out", box / "policy.toml", user_scope=scope)
        assert "defaultMode" not in files["settings.json"]
    src = (Path(status.__file__).parent / "adapters" / "claude_code.py").read_text(encoding="utf-8")
    assert "defaultMode" not in src  # nothing in the adapter names it


def test_the_state_dir_follows_the_environment_not_the_real_home(box, capsys, monkeypatch):
    other = box / "elsewhere-state"
    monkeypatch.setenv("WS_HOME", str(other))
    gen = put(other / "gate" / "claude-code" / "settings.json", {"sandbox": BLOCK})
    assert data_of(capsys)["sandbox"]["source"] == str(gen)
