"""The gate in every session: ws's items merged into the runner's user config beside the person's own, recorded, and
taken out exactly. Everything runs under a tmp HOME and WS_HOME; the real ~/.claude is never read or written."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess

import pytest

from ws import cli, doctor, install, osinfo, policy, selfcmd, userconfig


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def refused(capsys, box, *extra, runner="claude-code"):
    """Run `ws install --json` and return the error envelope; the install must have failed."""
    argv = [
        "install",
        "--yes",
        "--json",
        "--notes",
        str(box / "Notes"),
        *(["--runner", runner] if runner else []),
        *extra,
    ]
    code = cli.main(argv)
    out = capsys.readouterr().out
    assert code == 1, out
    return json.loads(out)


def do_install(capsys, box, *extra, runner="claude-code"):
    return run(capsys, "install", "--yes", "--runner", runner, "--notes", str(box / "Notes"), *extra)


MINE = {
    "model": "opus",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine-pre"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "echo mine-stop"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo mine-start"}]}],
    },
    "permissions": {"deny": ["Read(~/.private/**)", "Bash(curl:*)"], "allow": ["Bash(ls:*)"]},
    "env": {"FOO": "1", "CAFÉ": "☕"},
    "statusLine": {"type": "command", "command": "echo hi"},
    "aFutureKey": [1, 2, {"a": None, "b": 1.5}],
}


def plant(box, doc=None, indent=4, newline=True):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir(exist_ok=True)
    text = json.dumps(MINE if doc is None else doc, indent=indent, ensure_ascii=False) + ("\n" if newline else "")
    f.write_bytes(text.encode("utf-8"))
    return f


def settings(box):
    return json.loads((box / ".claude" / "settings.json").read_text(encoding="utf-8"))


def manifest(box):
    return json.loads((box / ".ws" / "install-manifest.json").read_text(encoding="utf-8"))


def merge_entry(box):
    return next(e for e in manifest(box)["entries"] if e.get("kind") == "merge")


# ---- merging into a person's file ------------------------------------------------------------------------------------


def test_ws_items_go_in_beside_yours_and_nothing_of_yours_moves(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    code, _, _ = do_install(capsys, box)
    assert code == 0
    got = settings(box)
    for key in ("model", "statusLine", "aFutureKey"):
        assert got[key] == MINE[key]
    assert list(got)[: len(MINE)] == list(MINE)  # your keys keep their order; ws's come after
    for event, groups in MINE["hooks"].items():
        assert got["hooks"][event][: len(groups)] == groups  # yours first, ws's appended
    assert len(got["hooks"]["PreToolUse"]) > 1 and len(got["hooks"]["SessionStart"]) > 1
    assert got["permissions"]["deny"][:2] == MINE["permissions"]["deny"]
    assert len(got["permissions"]["deny"]) > 2
    assert got["permissions"]["allow"] == ["Bash(ls:*)"]
    assert got["env"]["FOO"] == "1" and got["env"]["CAFÉ"] == "☕"
    assert "WS_GATED_SESSION" not in got["env"]  # it would block `ws approve` in your own Claude shell
    assert got["env"]["WS_SANDBOXED"] == "0"  # no sandbox block here, so the hook skips the project-settings preflight
    assert "sandbox" not in got
    text = f.read_text(encoding="utf-8")
    assert text.startswith('{\n    "model"') and text.endswith("\n")  # your indentation
    backup = next((box / ".ws" / "backups").iterdir())
    assert backup.name.startswith("claude-code-user-config-") and backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (box / ".ws" / "backups").stat().st_mode & 0o077 == 0


def test_the_hook_command_is_this_python_never_a_bare_ws(box, capsys):
    do_install(capsys, box)
    commands = [h["command"] for g in settings(box)["hooks"]["PreToolUse"] for h in g["hooks"]]
    ws = next(c for c in commands if "gate check" in c)
    assert ws.startswith(shlex.join(selfcmd.argv()))


def test_a_second_install_reports_0_changes_and_leaves_every_byte(box, capsys):
    f = plant(box)
    do_install(capsys, box)
    after = f.read_bytes()
    code, out, _ = do_install(capsys, box)
    assert code == 0 and "0 changes" in out
    assert f.read_bytes() == after
    assert len(list((box / ".ws" / "backups").iterdir())) == 1


def test_uninstall_after_an_untouched_install_gives_back_your_file_byte_for_byte(box, capsys):
    f = plant(box, indent=3, newline=False)
    original = f.read_bytes()
    do_install(capsys, box)
    assert f.read_bytes() != original
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "restored" in out
    assert f.read_bytes() == original
    assert not (box / ".ws" / "backups").exists()
    assert not (box / ".ws" / "install-manifest.json").exists()


def test_uninstall_dry_run_says_what_it_would_restore_and_changes_nothing(box, capsys):
    f = plant(box)
    do_install(capsys, box)
    merged = f.read_bytes()
    backups = sorted((box / ".ws" / "backups").iterdir())
    code, out, _ = run(capsys, "uninstall", "--dry-run")
    assert code == 0 and "would restore" in out
    assert f.read_bytes() == merged and sorted((box / ".ws" / "backups").iterdir()) == backups


def test_accept_three_hooks_and_two_deny_rules_install_install_uninstall(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    assert do_install(capsys, box)[0] == 0
    assert "0 changes" in do_install(capsys, box)[1]
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert f.read_bytes() == original


def test_a_missing_file_is_made_and_uninstall_takes_it_and_its_folder_away(box, capsys):
    assert not (box / ".claude").exists()
    do_install(capsys, box)
    assert (box / ".claude" / "settings.json").is_file()
    assert merge_entry(box)["backup"] is None
    run(capsys, "uninstall", "--yes")
    assert not (box / ".claude").exists()


@pytest.mark.parametrize("content", [b"", b"  \n\n"])
def test_an_empty_file_is_merged_into_and_comes_back_empty(box, capsys, content):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_bytes(content)
    do_install(capsys, box)
    assert "hooks" in settings(box)
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == content


def test_a_file_you_edited_since_loses_only_ws_items_and_keeps_your_edits(box, capsys):
    f = plant(box)
    do_install(capsys, box)
    doc = settings(box)
    doc["model"] = "sonnet"
    doc["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "echo added-later"}]})
    doc["permissions"]["deny"].append("Bash(wget:*)")
    doc["brandNew"] = True
    f.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "kept your edits" in out
    got = settings(box)
    assert got["model"] == "sonnet" and got["brandNew"] is True
    assert got["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "echo added-later"}]},
        *MINE["hooks"]["Stop"],
    ]
    assert got["hooks"]["PreToolUse"] == MINE["hooks"]["PreToolUse"]
    assert got["permissions"]["deny"] == [*MINE["permissions"]["deny"], "Bash(wget:*)"]
    assert got["env"] == MINE["env"]
    assert "UserPromptSubmit" not in got["hooks"]  # a list ws made and left empty goes
    assert list((box / ".ws" / "backups").iterdir())  # the backup stays when the file changed


def test_a_policy_change_swaps_ws_items_in_place_and_undo_is_still_exact(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    do_install(capsys, box)
    first = settings(box)["permissions"]["deny"]
    assert "WebFetch" not in first
    pol = box / ".ws" / "policy.toml"
    pol.write_text(pol.read_text(encoding="utf-8").replace("deny_tools = []", 'deny_tools = ["web_fetch"]'))
    code, out, _ = do_install(capsys, box)
    assert code == 0 and "changed" in out
    after = settings(box)["permissions"]["deny"]
    assert after[:2] == MINE["permissions"]["deny"] and "WebFetch" in after
    assert [r for r in after if r != "WebFetch"] == first  # same rules, same places
    assert len(list((box / ".ws" / "backups").iterdir())) == 1  # still the one original
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == original


def test_a_hook_that_ws_added_is_found_by_exact_equality_not_a_marker(box, capsys):
    do_install(capsys, box)
    for groups in settings(box)["hooks"].values():
        for g in groups:
            assert set(g) <= {"matcher", "hooks"}  # nothing ws-only that Claude Code's validation could reject
    e = merge_entry(box)
    assert e["sha256"] and e["runner"] == "claude-code" and e["added"]
    assert all(op["op"] in ("append", "set") for op in e["added"])


def test_an_item_you_already_had_is_not_ws_s_to_remove(box, capsys):
    mine = json.loads(json.dumps(MINE))
    mine["permissions"]["deny"].append("WebFetch")
    plant(box)
    pol = box / ".ws"
    pol.mkdir()
    (pol / "policy.toml").write_text(
        policy.default_toml().replace("deny_tools = []", 'deny_tools = ["web_fetch"]'), encoding="utf-8"
    )
    f = plant(box, mine)
    do_install(capsys, box)
    assert settings(box)["permissions"]["deny"].count("WebFetch") == 1
    assert not any(op["value"] == "WebFetch" for op in merge_entry(box)["added"])
    doc = settings(box)
    doc["model"] = "sonnet"  # edited, so the undo is the surgical one
    f.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    run(capsys, "uninstall", "--yes")
    assert "WebFetch" in settings(box)["permissions"]["deny"]  # yours stays


# ---- refusals --------------------------------------------------------------------------------------------------------


def test_an_env_key_you_set_to_another_value_is_a_conflict_and_nothing_changes(box, capsys):
    mine = {**MINE, "env": {"WS_SANDBOXED": "1"}}
    f = plant(box, mine)
    original = f.read_bytes()
    e = refused(capsys, box, runner="claude-code")
    assert e["error_kind"] == "conflict" and "env.WS_SANDBOXED" in e["message"]
    assert "ws gate launch" in e["hint"]
    assert f.read_bytes() == original
    assert not (box / ".ws").exists() and not (box / "Notes").exists()  # nothing at all was written


def test_a_link_is_refused_with_the_way_to_gate_a_session_instead(box, capsys):
    real = box / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    (box / ".claude").mkdir()
    (box / ".claude" / "settings.json").symlink_to(real)
    e = refused(capsys, box, runner="claude-code")
    assert e["error_kind"] == "refused" and "is a link" in e["message"] and "ws gate launch" in e["hint"]
    assert real.read_text() == "{}\n"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ('{\n  // my notes\n  "model": "opus"\n}\n', "not plain JSON"),
        ('{"a": 1,}', "not plain JSON"),
        ("[1, 2]", "not a JSON object"),
        ("not json", "not plain JSON"),
    ],
)
def test_a_file_that_is_not_plain_json_is_refused_and_left_alone(box, capsys, text, why):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(text)
    e = refused(capsys, box, runner="claude-code")
    assert e["error_kind"] == "refused" and why in e["message"] and "ws gate launch" in e["hint"]
    assert f.read_text() == text


def test_a_hooks_key_that_is_not_an_object_is_refused(box, capsys):
    f = plant(box, {"hooks": ["oops"]})
    e = refused(capsys, box, runner="claude-code")
    assert "`hooks` is not an object" in e["message"]
    assert json.loads(f.read_text()) == {"hooks": ["oops"]}


def test_claude_config_dir_moves_the_target(box, capsys, monkeypatch):
    elsewhere = box / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    do_install(capsys, box)
    assert (elsewhere / "settings.json").is_file() and not (box / ".claude").exists()


# ---- the profile setting ---------------------------------------------------------------------------------------------


def profile_text(box):
    return (box / ".ws" / "profile.toml").read_text(encoding="utf-8")


def test_a_new_profile_gets_every_session(box, capsys):
    do_install(capsys, box)
    assert 'gate = "every-session"' in profile_text(box)


def test_a_profile_that_had_a_runner_before_the_setting_stays_on_launch(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "profile.toml").write_text('[roles.runner]\nadapter = "claude-code"\n')
    f = plant(box)
    original = f.read_bytes()
    code, _, _ = do_install(capsys, box)
    assert code == 0 and f.read_bytes() == original
    assert "gate" not in profile_text(box).split("[roles.runner]")[1].split("[")[0]
    assert not (box / ".ws" / "backups").exists()
    assert "0 changes" in do_install(capsys, box)[1]


def test_gate_launch_takes_ws_out_and_every_session_puts_it_back(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    do_install(capsys, box)
    code, _, _ = do_install(capsys, box, "--gate", "launch")
    assert code == 0
    assert f.read_bytes() == original
    assert 'gate = "launch"' in profile_text(box)
    assert "gated when started with `ws gate launch`" in userconfig.check(os.environ)[1]
    _, _, _ = do_install(capsys, box, "--gate", "every-session")
    assert "PreToolUse" in settings(box)["hooks"]
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == original


def test_gate_needs_a_runner(box, capsys):
    e = refused(capsys, box, "--gate", "launch", runner="")
    assert "--gate needs a runner" in e["message"]


def test_a_bad_gate_in_the_profile_is_one_clear_error(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "profile.toml").write_text('[roles.runner]\nadapter = "claude-code"\ngate = "sometimes"\n')
    e = refused(capsys, box, runner="")
    assert "gate must be one of" in e["message"]


# ---- the manifest cannot aim ws at another file ----------------------------------------------------------------------


def test_a_manifest_that_points_the_merge_elsewhere_is_refused(box, capsys):
    f = plant(box)
    do_install(capsys, box)
    other = box / "precious.json"
    other.write_text('{"keep": true}\n')
    path = box / ".ws" / "install-manifest.json"
    doc = json.loads(path.read_text())
    for e in doc["entries"]:
        if e.get("kind") == "merge":
            e["path"] = str(other)
    path.write_text(json.dumps(doc))
    _, out, _ = run(capsys, "uninstall", "--yes")
    assert other.read_text() == '{"keep": true}\n'
    assert str(other) in out
    assert "PreToolUse" in settings(box)["hooks"]
    assert f.is_file()


def test_uninstall_that_cannot_read_the_file_now_keeps_its_record_for_a_retry(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    do_install(capsys, box)
    broken = f.read_bytes()
    f.write_text('{"broken": ')
    _, _, err = run(capsys, "uninstall", "--yes")
    assert "ws's entries are still in it" in err
    assert merge_entry(box)["added"]
    f.write_bytes(broken)
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == original


# ---- the hook ---------------------------------------------------------------------------------------------------------


def hook_command(box):
    return next(
        h["command"] for g in settings(box)["hooks"]["PreToolUse"] for h in g["hooks"] if "gate check" in h["command"]
    )


def run_hook(box, command, call):
    payload = {"tool_name": "Bash", "tool_input": {"command": call}, "session_id": "s1", "cwd": str(box)}
    env = {**os.environ, "HOME": str(box), "WS_HOME": str(box / ".ws")}
    return subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env=env,
        cwd=box,
        timeout=60,
    )


def test_the_merged_hook_denies_rm_rf_and_allows_a_plain_command(box, capsys):
    plant(box)
    do_install(capsys, box)
    cmd = hook_command(box)
    denied = run_hook(box, cmd, "rm -rf /")
    assert denied.returncode == 2
    assert "the gate did not run" not in denied.stderr  # a deny is the gate working
    assert run_hook(box, cmd, "ls").returncode == 0


def test_when_ws_cannot_start_the_hook_blocks_and_names_the_backup_and_the_way_out(box, capsys):
    plant(box)
    do_install(capsys, box)
    backup = next((box / ".ws" / "backups").iterdir())
    cmd = hook_command(box).replace(shlex.quote(selfcmd.argv()[0]), "/nonexistent/python", 1)
    if "/nonexistent/python" not in cmd:
        cmd = hook_command(box).replace(selfcmd.argv()[0], "/nonexistent/python", 1)
    res = run_hook(box, cmd, "ls")
    assert res.returncode == 2
    assert "the gate did not run" in res.stderr
    assert "install --gate launch" in res.stderr and str(backup) in res.stderr


def test_the_session_files_ws_makes_carry_the_same_hooks_so_a_gated_launch_runs_each_once(box, capsys):
    do_install(capsys, box)
    wide = json.loads((box / ".ws" / "gate" / "claude-code" / "settings.json").read_text())
    ours = settings(box)["hooks"]
    for event, groups in wide["hooks"].items():
        assert groups == ours[event][-len(groups) :], event  # identical handlers: Claude Code runs a repeat once
    assert wide["sandbox"]["enabled"] is True  # the launch file still carries the wall


def test_the_target_is_protected_from_a_session_s_own_edits(box, capsys):
    do_install(capsys, box)
    target = str(box / ".claude" / "settings.json")
    assert f"Edit(/{target})" in settings(box)["permissions"]["deny"]
    assert shlex.quote(target) in hook_command(box) or target in hook_command(box)


# ---- OpenCode --------------------------------------------------------------------------------------------------------


def opencode_file(box):
    return box / ".config" / "opencode" / "opencode.json"


def test_opencode_gets_the_plugin_and_ws_deny_keys_after_yours(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    mine = {
        "model": "x/y",
        "permission": {"bash": {"*": "ask", "git push*": "deny"}, "edit": {"*": "ask"}},
        "plugin": ["other"],
    }
    f.write_text(json.dumps(mine, indent=2) + "\n")
    original = f.read_bytes()
    code, _, _ = do_install(capsys, box, runner="opencode")
    assert code == 0
    got = json.loads(f.read_text())
    assert got["model"] == "x/y" and got["plugin"] == ["other"]
    bash = list(got["permission"]["bash"].items())
    assert bash[:2] == [("*", "ask"), ("git push*", "deny")]  # yours first and untouched
    assert len(bash) > 2 and all(v == "deny" for _, v in bash[2:])  # ws's deny keys last, so they win
    assert next(iter(got["permission"]["edit"].items())) == ("*", "ask")
    assert (box / ".config" / "opencode" / "plugins" / "ws-gate.js").is_file()
    assert "0 changes" in do_install(capsys, box, runner="opencode")[1]
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == original
    assert not (box / ".config" / "opencode" / "plugins").exists()


def test_opencode_without_a_file_gets_one_and_loses_it_on_uninstall(box, capsys):
    do_install(capsys, box, runner="opencode")
    assert "permission" in json.loads(opencode_file(box).read_text())
    run(capsys, "uninstall", "--yes")
    assert not (box / ".config").exists()


def test_opencode_jsonc_or_comments_are_refused(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    (f.parent / "opencode.jsonc").write_text("{}\n")
    e = refused(capsys, box, runner="opencode")
    assert e["error_kind"] == "refused" and "ws gate launch" in e["hint"]
    (f.parent / "opencode.jsonc").unlink()
    f.write_text('{\n // note\n "model": "x"\n}\n')
    refused(capsys, box, runner="opencode")


def test_opencode_rules_of_yours_that_contradict_ws_are_a_conflict(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    key = policy.default_policy().gate.deny_commands[0]
    f.write_text(json.dumps({"permission": {"bash": {key: "allow"}}}))
    e = refused(capsys, box, runner="opencode")
    assert e["error_kind"] == "conflict" and key in e["message"]
    f.write_text(json.dumps({"permission": "ask"}))
    e = refused(capsys, box, runner="opencode")
    assert "`permission` is not an object" in e["message"]


# ---- WS008 ------------------------------------------------------------------------------------------------------------


def ws008(box):
    checks = doctor.run_checks(osinfo.detect(), box / ".ws" / "profile.toml", env=dict(os.environ), network=False)
    return next(c for c in checks if c.id == "WS008")


def test_ws008_every_session_and_current_is_ok(box, capsys):
    do_install(capsys, box)
    c = ws008(box)
    assert c.status == "ok" and "every session is gated" in c.detail and c.role == "gate"


def test_ws008_launch_is_ok_and_says_how(box, capsys):
    do_install(capsys, box, "--gate", "launch")
    c = ws008(box)
    assert c.status == "ok" and "ws gate launch" in c.detail


def test_ws008_without_a_runner_is_ok(box):
    assert userconfig.check(os.environ)[0] == "ok"


def test_ws008_a_changed_policy_is_stale_and_says_run_install(box, capsys):
    do_install(capsys, box)
    pol = box / ".ws" / "policy.toml"
    pol.write_text(pol.read_text(encoding="utf-8").replace("deny_tools = []", 'deny_tools = ["web_fetch"]'))
    c = ws008(box)
    assert c.status == "warn" and "earlier policy" in c.detail and "ws install" in c.fix
    do_install(capsys, box)
    assert ws008(box).status == "ok"


def test_ws008_entries_taken_out_by_hand_fail_with_the_command_to_put_them_back(box, capsys):
    f = plant(box)
    do_install(capsys, box)
    doc = settings(box)
    doc["hooks"].pop("PreToolUse")
    f.write_text(json.dumps(doc, indent=4) + "\n")
    c = ws008(box)
    assert c.status == "fail" and "gone" in c.detail and "ws install" in c.fix
    f.unlink()
    assert ws008(box).status == "fail"


def test_ws008_a_hook_command_that_no_longer_starts_fails(box, capsys):
    do_install(capsys, box)
    path = box / ".ws" / "install-manifest.json"
    doc = json.loads(path.read_text())
    for e in doc["entries"]:
        if e.get("kind") == "merge":
            e["cmd"] = ["/nonexistent/python", "-I", "-m", "ws"]
    path.write_text(json.dumps(doc))
    c = ws008(box)
    assert c.status == "fail" and "no longer starts" in c.detail and "ws install" in c.fix


def test_ws008_gate_every_session_with_no_entries_fails(box, capsys):
    do_install(capsys, box)
    run(capsys, "uninstall", "--yes", "--dry-run")
    (box / ".ws" / "install-manifest.json").unlink()
    assert ws008(box).status == "fail"


def test_ws008_names_each_guard_the_runner_does_not_run(box, capsys):
    do_install(capsys, box, runner="opencode")
    c = ws008(box)
    assert c.status == "warn"
    from ws.adapters import opencode

    on = [g for g in opencode.UNSUPPORTED_GUARDS if g in policy.default_policy().guards.on]
    assert on and all(f"`{g}` isn't run by opencode" in c.detail for g in on)


def test_ws008_a_launch_profile_with_ws_items_still_there_warns(box, capsys):
    do_install(capsys, box)
    (box / ".ws" / "profile.toml").write_text(
        profile_text(box).replace('gate = "every-session"', 'gate = "launch"'), encoding="utf-8"
    )
    assert ws008(box).status == "warn"


# ---- the admin list ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "ws uninstall",
        "ws uninstall --yes",
        "ws install --gate launch",
        "ws install --yes --gate=launch",
        "ws install --ga launch --runner claude-code",
        "ws skills adopt align",
        "ws skills restore align",
        "ws config fallback",
        "python3 -I -m ws uninstall --yes",
        "uv run ws install --gate launch",
        "bash -c 'ws uninstall'",
    ],
)
def test_a_gated_session_cannot_take_off_its_own_gate(cmd):
    assert "gated session" in (policy.is_denied_command(policy.loads(""), cmd) or ""), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "ws install",
        "ws install --yes --runner claude-code",
        "ws install --gate every-session",
        "ws install --gate=every-session",
        "ws skills list",
        "ws config show",
        "echo ws uninstall",
    ],
)
def test_a_plain_install_and_the_rest_stay_allowed(cmd):
    assert policy.is_denied_command(policy.loads(""), cmd) is None, cmd


# ---- the engine on its own --------------------------------------------------------------------------------------------


def test_update_swaps_in_place_and_removes_what_is_left():
    doc = {"hooks": {"Stop": [{"a": 1}, {"ws": 1}, {"b": 2}]}, "env": {"K": "1", "Z": "z"}}
    old = [
        {"op": "append", "path": ["hooks", "Stop"], "value": {"ws": 1}},
        {"op": "set", "path": ["env", "K"], "value": "1"},
    ]
    new_ops = [{"op": "append", "path": ["hooks", "Stop"], "value": {"ws": 2}}]
    from pathlib import Path

    got, added, _ = userconfig.update(doc, old, [], new_ops, Path("x"))
    assert got == {"hooks": {"Stop": [{"a": 1}, {"ws": 2}, {"b": 2}]}, "env": {"Z": "z"}}
    assert added == new_ops
    assert doc["env"]["K"] == "1"  # the input is not changed


def test_install_module_still_lists_a_merge_as_a_step_kind(box):
    steps = install.plan(install.Answers(notes=str(box / "Notes"), runner="claude-code"))
    merges = [s for s in steps if s.kind == "merge"]
    assert len(merges) == 1 and merges[0].path == box / ".claude" / "settings.json"


# ---- a re-merge after the file changed -------------------------------------------------------------------------------


def edit(box, **keys):
    doc = settings(box)
    doc.update(keys)
    (box / ".claude" / "settings.json").write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")


def tighten_policy(box):
    pol = box / ".ws" / "policy.toml"
    pol.write_text(pol.read_text(encoding="utf-8").replace("deny_tools = []", 'deny_tools = ["web_fetch"]'))


def test_a_re_merge_after_you_edited_the_file_drops_the_exact_restore(box, capsys):
    plant(box)
    do_install(capsys, box)
    edit(box, model="sonnet", brandNew=True)  # between two installs
    tighten_policy(box)
    do_install(capsys, box)
    assert merge_entry(box)["exact"] is False
    assert len(list((box / ".ws" / "backups").iterdir())) == 1  # still the one saved copy
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "kept your edits" in out
    got = settings(box)
    assert got["model"] == "sonnet" and got["brandNew"] is True  # the edits made in between stay
    assert got["permissions"]["deny"] == MINE["permissions"]["deny"]
    assert got["hooks"] == MINE["hooks"] and got["env"] == MINE["env"]


def test_a_file_ws_made_that_gained_content_is_not_deleted_by_a_later_undo(box, capsys):
    do_install(capsys, box)
    assert merge_entry(box)["created_by_ws"] is True
    edit(box, model="sonnet")
    tighten_policy(box)
    do_install(capsys, box)
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "kept your edits" in out
    assert settings(box) == {"model": "sonnet"}


def test_a_re_merge_of_an_unchanged_file_keeps_the_exact_restore(box, capsys):
    f = plant(box)
    original = f.read_bytes()
    do_install(capsys, box)
    tighten_policy(box)
    do_install(capsys, box)
    assert merge_entry(box)["exact"] is True
    run(capsys, "uninstall", "--yes")
    assert f.read_bytes() == original


# ---- no test writes into a real config ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG"],
)
def test_the_suite_starts_every_test_without_a_runner_config_variable(name):
    assert name not in os.environ  # tests/conftest.py takes them out, whatever the shell running pytest had


# ---- what every session locks ---------------------------------------------------------------------------------------------


def edit_rule_matches(rule: str, path: str) -> bool:
    """Claude Code's gitignore-style `Edit(...)` rule against an absolute path (`//x` is the absolute path /x)."""
    spec = rule[len("Edit(") : -1]
    if spec.startswith("//"):
        return re.fullmatch(re.escape(spec[1:]) + r"(/.*)?", path) is not None
    out, i = "", 0
    while i < len(spec):
        if spec.startswith("**/", i):
            out += "(.*/)?"
            i += 3
        elif spec.startswith("**", i):
            out += ".*"
            i += 2
        elif spec[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(spec[i])
            i += 1
    return re.fullmatch(".*/" + out, path) is not None


def edit_rules(doc):
    return [r for r in doc["permissions"]["deny"] if r.startswith("Edit(")]


def test_every_session_locks_only_the_files_that_run_as_code(box, capsys):
    do_install(capsys, box)
    rules = edit_rules(settings(box))
    for want in (
        "Edit(**/.claude/settings.json)",
        "Edit(**/.claude/settings.local.json)",
        "Edit(**/.mcp.json)",
        "Edit(**/.git/hooks/**)",
        "Edit(**/.git/config)",
    ):
        assert want in rules
    for wide in ("Edit(**/.claude/**)", "Edit(**/.claude)", "Edit(**/.git/**)", "Edit(**/opencode.json)"):
        assert wide not in rules


def test_an_edit_in_a_claude_worktree_is_matched_by_no_every_session_deny_rule(box, capsys):
    do_install(capsys, box)
    rules = edit_rules(settings(box))
    repo = "/work/repo"
    for ok in (
        f"{repo}/.claude/worktrees/x/src/a.py",
        f"{repo}/.claude/worktrees/x/README.md",
        f"{repo}/src/a.py",
        f"{repo}/.claude/skills/mine/SKILL.md",
        f"{repo}/.claude/worktrees/x/.claude/commands/c.md",
    ):
        assert not [r for r in rules if edit_rule_matches(r, ok)], ok
    for locked in (
        f"{repo}/.claude/settings.json",
        f"{repo}/.claude/worktrees/x/.claude/settings.local.json",
        f"{repo}/.mcp.json",
        f"{repo}/.git/hooks/pre-commit",
        f"{repo}/.git/config",
        str(box / ".ws" / "policy.toml"),
        str(box / ".claude" / "settings.json"),
    ):
        assert [r for r in rules if edit_rule_matches(r, locked)], locked


def test_the_interpreter_tree_the_hook_runs_from_is_locked(box, capsys, monkeypatch):
    tree = box / "tools" / "ws-env"
    monkeypatch.setattr(selfcmd, "tree", lambda: tree)
    do_install(capsys, box)
    rules = edit_rules(settings(box))
    assert [r for r in rules if edit_rule_matches(r, str(tree / "lib" / "ws" / "gate.py"))]


def test_the_launch_file_keeps_the_wide_project_locks(box, capsys):
    do_install(capsys, box)
    wide = json.loads((box / ".ws" / "gate" / "claude-code" / "settings.json").read_text())
    rules = edit_rules(wide)
    assert "Edit(**/.claude/**)" in rules and "Edit(**/.git/hooks/**)" in rules
    assert [r for r in rules if edit_rule_matches(r, "/work/repo/.claude/worktrees/x/src/a.py")]


# ---- a hook interpreter that comes and goes ------------------------------------------------------------------------------

REAL_STABLE_CHECK = userconfig.require_stable_interpreter  # tests/conftest.py swaps it out while a test runs


@pytest.mark.parametrize(
    ("exe", "word"),
    [
        ("/home/me/code/proj/.venv/bin/python", ".venv"),
        ("/home/me/code/proj/.claude/worktrees/agent-1/.venv/bin/python", ".venv"),
        ("/home/me/code/proj/.claude/worktrees/agent-1/env/bin/python", "worktree"),
        ("/tmp/xyz/env/bin/python", "temp"),
        ("/var/tmp/env/bin/python", "temp"),
    ],
)
def test_an_interpreter_that_comes_and_goes_is_named(exe, word):
    assert word in (selfcmd.unstable([exe, "-I", "-m", "ws"]) or "")


@pytest.mark.parametrize(
    "exe",
    [
        "/home/me/.local/share/pipx/venvs/ws/bin/python",
        "/home/me/.local/share/uv/tools/ws/bin/python",
        "/usr/bin/python3",
        "/opt/ws/bin/python",
    ],
)
def test_an_installed_interpreter_is_fine(exe):
    assert selfcmd.unstable([exe, "-I", "-m", "ws"]) is None


def test_a_tool_install_under_a_home_that_sits_in_a_temp_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    exe = str(tmp_path / ".local" / "share" / "uv" / "tools" / "ws-aitoolkit" / "bin" / "python")
    assert selfcmd.unstable([exe, "-I", "-m", "ws"]) is None
    assert ".venv" in (selfcmd.unstable([str(tmp_path / "code" / "p" / ".venv" / "bin" / "python")]) or "")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need rights on Windows")
def test_a_home_reached_through_a_link_still_counts_as_home(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "tools" / "bin").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path / "link"))
    assert selfcmd.unstable([str(real / "tools" / "bin" / "python"), "-I", "-m", "ws"]) is None


def test_every_session_refuses_a_project_venv_interpreter_and_writes_nothing(box, capsys, monkeypatch):
    monkeypatch.setattr(userconfig, "require_stable_interpreter", REAL_STABLE_CHECK)
    monkeypatch.setattr(selfcmd, "argv", lambda: [str(box / "proj" / ".venv" / "bin" / "python"), "-I", "-m", "ws"])
    e = refused(capsys, box)
    assert e["error_kind"] == "refused" and ".venv" in e["message"]
    assert "install.sh" in e["hint"] and "pipx" in e["hint"] and "uv tool" in e["hint"]
    assert not (box / ".claude").exists() and not (box / ".ws").exists()
    code, _, _ = do_install(capsys, box, "--gate", "launch")  # the wall alone needs no hook in your config
    assert code == 0 and not (box / ".claude" / "settings.json").exists()


def test_when_the_gate_cannot_run_the_message_says_how_to_get_unstuck(box, capsys):
    plant(box)
    do_install(capsys, box)
    cmd = hook_command(box).replace(selfcmd.argv()[0], "/nonexistent/python", 1)
    res = run_hook(box, cmd, "ls")
    assert res.returncode == 2
    assert "run `ws install --gate launch`" in res.stderr
    assert "delete the PreToolUse group whose command contains `gate check`" in res.stderr
    assert str(box / ".claude" / "settings.json") in res.stderr


# ---- one writer at a time, and a checked write ---------------------------------------------------------------------------


def test_the_lock_file_is_gone_after_an_install_and_an_uninstall(box, capsys):
    plant(box)
    do_install(capsys, box)
    assert not [p for p in (box / ".claude").iterdir() if "lock" in p.name]
    run(capsys, "uninstall", "--yes")
    assert not [p for p in (box / ".claude").iterdir() if "lock" in p.name]


def test_an_install_waits_for_the_lock_then_refuses_and_changes_nothing(box, capsys, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    f = plant(box)
    original = f.read_bytes()
    monkeypatch.setattr(userconfig, "_LOCK_WAIT", 0.2)
    fd = os.open(f.parent / ".settings.json.ws-lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        e = refused(capsys, box)
    finally:
        os.close(fd)
    assert e["error_kind"] == "conflict" and "another ws holds the lock" in e["message"]
    assert f.read_bytes() == original and not (box / ".ws" / "backups").exists()


def concurrent_writer(box, monkeypatch, times):
    """After ws has read the file and built its merge, someone else writes to it: `times` times."""
    real = userconfig.dump
    state = {"n": 0}

    def dump(doc, raw):
        out = real(doc, raw)
        if state["n"] < times:
            state["n"] += 1
            other = settings(box) if raw is not None else {}
            other[f"other{state['n']}"] = True
            (box / ".claude" / "settings.json").write_text(json.dumps(other, indent=4) + "\n", encoding="utf-8")
        return out

    monkeypatch.setattr(userconfig, "dump", dump)


def test_a_file_that_changes_under_ws_is_merged_again_once(box, capsys, monkeypatch):
    plant(box)
    concurrent_writer(box, monkeypatch, 1)
    code, _, _ = do_install(capsys, box)
    assert code == 0
    got = settings(box)
    assert got["other1"] is True and got["model"] == "opus"  # the other writer's key is in the file ws wrote
    assert len(got["hooks"]["PreToolUse"]) == 2
    backups = list((box / ".ws" / "backups").iterdir())
    assert len(backups) == 1  # the first attempt's saved copy was taken away again
    assert "other1" in backups[0].read_text()  # the one left holds the bytes ws merged into


def test_a_file_that_keeps_changing_is_refused_and_left_as_the_other_writer_left_it(box, capsys, monkeypatch):
    plant(box)
    concurrent_writer(box, monkeypatch, 5)
    e = refused(capsys, box)
    assert e["error_kind"] == "conflict" and "changed while ws was writing" in e["message"]
    got = settings(box)
    assert got["other2"] is True and len(got["hooks"]["PreToolUse"]) == 1  # no ws item went in
    assert not (box / ".ws" / "backups").exists()
    assert not [e for e in manifest(box)["entries"] if e.get("kind") == "merge"]  # nothing recorded for the file


# ---- a lost record ---------------------------------------------------------------------------------------------------------


def lose_the_record(box):
    (box / ".ws" / "install-manifest.json").unlink()


def test_ws_items_with_no_record_are_adopted_so_an_undo_takes_them_out(box, capsys):
    plant(box)
    do_install(capsys, box)
    lose_the_record(box)
    assert ws008(box).status == "fail"
    code, out, _ = do_install(capsys, box)
    assert code == 0 and "0 changes" not in out
    e = merge_entry(box)
    assert e["added"] and e["exact"] is False and e["created_by_ws"] is False
    assert ws008(box).status == "ok"
    assert len(settings(box)["hooks"]["PreToolUse"]) == 2  # no second copy of ws's hook
    assert "0 changes" in do_install(capsys, box)[1]
    run(capsys, "uninstall", "--yes")
    assert settings(box) == MINE


def test_a_stale_ws_hook_with_no_record_is_updated_in_place(box, capsys):
    plant(box)
    do_install(capsys, box)
    lose_the_record(box)
    doc = settings(box)
    hook = doc["hooks"]["PreToolUse"][-1]["hooks"][0]
    hook["command"] = hook["command"].replace(selfcmd.argv()[0], "/old/ws/bin/python", 1)
    (box / ".claude" / "settings.json").write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    assert "/old/ws/bin/python" in hook_command(box)
    do_install(capsys, box)
    assert "/old/ws/bin/python" not in hook_command(box)
    assert len(settings(box)["hooks"]["PreToolUse"]) == 2
    run(capsys, "uninstall", "--yes")
    assert settings(box) == MINE


def test_a_file_with_no_ws_items_and_no_record_is_a_normal_first_install(box, capsys):
    plant(box)
    do_install(capsys, box)
    e = merge_entry(box)
    assert e["exact"] is True and e["backup"]


# ---- WS008 lists allow rules that ws's deny rules override ------------------------------------------------------------


def ws008_rows(box):
    checks = doctor.run_checks(osinfo.detect(), box / ".ws" / "profile.toml", env=dict(os.environ), network=False)
    return [c for c in checks if c.id == "WS008"]


def test_ws008_lists_each_allow_rule_that_a_ws_deny_rule_overrides(box, capsys):
    do_install(capsys, box)
    doc = settings(box)
    doc["permissions"]["allow"] = ["Bash(rm -rf /etc/hosts)", "Bash(ls:*)", "Bash(npm test:*)"]
    (box / ".claude" / "settings.json").write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    rows = ws008_rows(box)
    over = [c for c in rows if "overridden" in c.detail]
    assert len(over) == 1 and over[0].status == "warn"
    assert "`Bash(rm -rf /etc/hosts)`" in over[0].detail and "deny rule `Bash(rm -* /etc*)`" in over[0].detail
    assert "allow rule" in over[0].fix
    assert rows[0].status == "ok"  # the gate itself is in place


def test_ws008_has_no_override_lines_when_nothing_overlaps(box, capsys):
    plant(box)
    do_install(capsys, box)
    assert len(ws008_rows(box)) == 1


@pytest.mark.parametrize(
    ("allow", "deny", "hit"),
    [
        ("WebFetch", "WebFetch", True),
        ("WebFetch(domain:example.com)", "WebFetch", True),
        ("Bash(git push:*)", "Bash(git push*)", True),
        ("Bash(git push origin main)", "Bash(git push:*)", True),
        ("Bash(git:*)", "Bash(git push:*)", True),
        ("Bash(ls:*)", "Bash(rm -* /)", False),
        ("Read(~/notes/**)", "Bash(rm -* /)", False),
        ("Bash", "Bash(rm -* /)", False),
    ],
)
def test_which_allow_rules_a_deny_rule_overrides(allow, deny, hit):
    assert userconfig._overrides(allow, deny) is hit


# ---- ws approve from your own Claude shell ----------------------------------------------------------------------------------


def test_the_merged_env_does_not_stop_ws_approve_in_your_own_claude_shell(box, capsys):
    from ws import approvals

    do_install(capsys, box)
    env = settings(box)["env"]  # what Claude Code puts into the shell a person's `!` command runs in
    assert approvals.refuse_reason(dict(env), True) is None
    assert "not for a gated session" in policy.is_denied_command(policy.loads(""), "ws approve abc")  # the model's call


def test_a_test_that_undoes_its_monkeypatch_still_has_no_runner_config_variable(monkeypatch):
    monkeypatch.undo()  # test_modules does this; it must not bring the shell's variables back
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "OPENCODE_CONFIG"):
        assert name not in os.environ
