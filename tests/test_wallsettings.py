"""The wall's settings: `ws install` refuses to merge beside settings that open the sandbox unless the person keeps them on
purpose (`--allow-widened-sandbox`, recorded), and `ws doctor` WS012 reports what the files say, never "ok" while
something widens the wall or the `denyRead` list is stale. Everything runs under a tmp HOME and WS_HOME; the managed
settings files are pointed at a tmp folder by tests/conftest.py."""

from __future__ import annotations

import json

import pytest

from ws import cli, install, userconfig, wallsettings

SSH = {"sandbox": {"filesystem": {"allowRead": ["~/.ssh"]}}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        wallsettings, "host_problem", lambda kind, which=None: None
    )  # bubblewrap or not, the file is read
    monkeypatch.chdir(tmp_path)
    return tmp_path


def user_file(box, doc: dict | None = None):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir(exist_ok=True)
    if doc is not None:
        f.write_text(json.dumps(doc))
    return f


def run_install(capsys, box, *extra):
    """(exit code, the JSON error or result)."""
    code = cli.main(["install", "--yes", "--json", "--runner", "claude-code", "--notes", str(box / "Notes"), *extra])
    return code, json.loads(capsys.readouterr().out)


def ws012(box):
    return wallsettings.doctor_check(dict(__import__("os").environ), "linux")


def entry(box) -> dict:
    return install._read_manifest(dict(__import__("os").environ))[0][str(box / ".claude" / "settings.json")]


# --- the review's two states -------------------------------------------------------------------------------------------


def test_a_settings_file_with_an_excluded_command_and_an_allowed_read_is_refused_and_not_changed(box, capsys):
    mine = {"sandbox": {"excludedCommands": ["git:*"], "filesystem": {"allowRead": ["~/.ssh"]}}}
    f = user_file(box, mine)
    before = f.read_bytes()
    code, out = run_install(capsys, box)
    assert code == 1 and out["error_kind"] == "refused"
    assert (
        "sandbox.excludedCommands git:*" in out["message"] and "sandbox.filesystem.allowRead ~/.ssh" in out["message"]
    )
    assert str(f) in out["message"]  # the file, for each
    assert "--allow-widened-sandbox" in out["hint"] and "ws install" in out["hint"]
    assert f.read_bytes() == before
    assert not (box / ".ws" / "backups").exists() or not list((box / ".ws" / "backups").iterdir())


def test_ws012_is_not_ok_when_the_file_holds_the_widening_keys_and_says_which(box, capsys):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    assert ws012(box)[0] == "ok"
    f = box / ".claude" / "settings.json"
    conf = json.loads(f.read_text())
    conf["sandbox"]["excludedCommands"] = ["git:*"]
    conf["sandbox"]["filesystem"]["allowRead"] = ["~/.ssh"]
    f.write_text(json.dumps(conf))
    status, detail, fix = ws012(box)
    assert status == "warn" and detail.startswith("wall widened by user: ")
    assert (
        "sandbox.excludedCommands git:*" in detail
        and "sandbox.filesystem.allowRead ~/.ssh" in detail
        and str(f) in detail
    )
    assert fix


def test_ws012_says_stale_when_a_deny_read_path_was_added_after_the_install(box, capsys):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    assert ws012(box)[0] == "ok"
    pf = box / ".ws" / "policy.toml"
    text = pf.read_text()
    assert "deny_read = [" in text
    pf.write_text(text.replace("deny_read = [", 'deny_read = ["~/.netrc", ', 1))
    status, detail, fix = ws012(box)
    assert status == "warn" and "stale" in detail and "ws install" in fix
    assert run_install(capsys, box)[0] == 0  # the install brings the list up to date
    assert ws012(box)[0] == "ok"


# --- each key ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "key"),
    [
        ({"sandbox": {"enabled": False}}, "sandbox.enabled"),
        ({"sandbox": {"excludedCommands": ["docker:*"]}}, "sandbox.excludedCommands docker:*"),
        ({"sandbox": {"allowUnsandboxedCommands": True}}, "sandbox.allowUnsandboxedCommands"),
        ({"sandbox": {"filesystem": {"disabled": True}}}, "sandbox.filesystem.disabled"),
        (SSH, "sandbox.filesystem.allowRead ~/.ssh"),
        ({"sandbox": {"filesystem": {"allowRead": ["~/.ssh/id_rsa"]}}}, "sandbox.filesystem.allowRead ~/.ssh/id_rsa"),
        ({"sandbox": {"filesystem": {"allowRead": ["~"]}}}, "sandbox.filesystem.allowRead ~"),
        ({"sandbox": {"filesystem": {"allowRead": ["$HOME/.ssh/**"]}}}, "sandbox.filesystem.allowRead $HOME/.ssh/**"),
        ({"sandbox": {"filesystem": {"allowWrite": ["~/.ws"]}}}, "sandbox.filesystem.allowWrite ~/.ws"),
        ({"sandbox": {"filesystem": {"allowWrite": ["./relative"]}}}, "sandbox.filesystem.allowWrite ./relative"),
    ],
)
def test_each_widening_key_is_refused_by_name(box, capsys, doc, key):
    f = user_file(box, doc)
    code, out = run_install(capsys, box)
    assert code == 1 and out["error_kind"] == "refused" and key in out["message"] and str(f) in out["message"]


@pytest.mark.parametrize(
    "doc",
    [
        {"sandbox": {"excludedCommands": []}},
        {"sandbox": {"enabled": True, "allowUnsandboxedCommands": False}},
        {"sandbox": {"filesystem": {"allowRead": ["~/work"], "allowWrite": ["~/.cache/mytool"]}}},
        {"model": "opus"},
    ],
)
def test_settings_that_leave_the_wall_whole_are_not_refused(box, capsys, doc):
    user_file(box, doc)
    code, _ = run_install(capsys, box)
    assert code == 0
    assert ws012(box)[0] == "ok"


def test_a_managed_settings_file_that_widens_the_wall_makes_ws012_not_ok(box, capsys, tmp_path, monkeypatch):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    assert ws012(box)[0] == "ok"
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"sandbox": {"excludedCommands": ["npm:*"]}}))
    monkeypatch.setattr(wallsettings, "MANAGED_FILES", (str(managed),))
    status, detail, _ = ws012(box)
    assert status == "warn" and "sandbox.excludedCommands npm:*" in detail and str(managed) in detail


def test_a_managed_drop_in_that_turns_the_sandbox_off_makes_ws012_not_ok(box, capsys, tmp_path, monkeypatch):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    drop = tmp_path / "managed-settings.d"
    drop.mkdir()
    (drop / "10-team.json").write_text(json.dumps({"sandbox": {"enabled": False}}))
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(drop),))
    status, detail, _ = ws012(box)
    assert status == "warn" and "sandbox.enabled" in detail and "10-team.json" in detail


# --- the named override ------------------------------------------------------------------------------------------------


def test_the_override_installs_beside_the_widening_and_records_the_choice(box, capsys):
    mine = {"sandbox": {"excludedCommands": ["git:*"], "filesystem": {"allowRead": ["~/.ssh"]}}}
    f = user_file(box, mine)
    code, _ = run_install(capsys, box, "--allow-widened-sandbox")
    assert code == 0
    conf = json.loads(f.read_text())
    assert conf["sandbox"]["excludedCommands"] == ["git:*"]  # yours stays
    assert conf["sandbox"]["filesystem"]["allowRead"] == ["~/.ssh"]
    assert conf["sandbox"]["enabled"] is True  # and ws's wall is merged beside it
    assert sorted(entry(box)["widened_by_user"]) == [
        "sandbox.excludedCommands git:*",
        "sandbox.filesystem.allowRead ~/.ssh",
    ]


def test_with_the_override_ws012_is_a_warn_that_lists_them_never_ok(box, capsys):
    user_file(box, SSH)
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    status, detail, _ = ws012(box)
    assert status == "warn" and detail.startswith("wall widened by user: ")
    assert "sandbox.filesystem.allowRead ~/.ssh" in detail


def test_the_recorded_choice_covers_a_later_install_but_not_a_new_widening(box, capsys):
    f = user_file(box, SSH)
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    assert run_install(capsys, box)[0] == 0  # the same setting, already agreed to
    conf = json.loads(f.read_text())
    conf["sandbox"]["excludedCommands"] = ["git:*"]
    f.write_text(json.dumps(conf))
    code, out = run_install(capsys, box)
    assert code == 1 and "sandbox.excludedCommands git:*" in out["message"]
    assert "allowRead" not in out["message"]  # only the new one is refused


def test_taking_the_widening_out_clears_the_record_and_ws012_is_ok_again(box, capsys):
    f = user_file(box, SSH)
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    conf = json.loads(f.read_text())
    conf["sandbox"]["filesystem"].pop("allowRead")
    f.write_text(json.dumps(conf))
    assert run_install(capsys, box)[0] == 0
    assert entry(box)["widened_by_user"] == []
    assert ws012(box)[0] == "ok"


def test_the_override_keeps_a_sandbox_you_switched_off_as_it_is_and_ws012_says_so(box, capsys):
    f = user_file(box, {"sandbox": {"enabled": False}})
    assert run_install(capsys, box)[0] == 1
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    assert json.loads(f.read_text())["sandbox"]["enabled"] is False
    status, detail, _ = ws012(box)
    assert status == "warn" and "sandbox.enabled" in detail


def test_the_override_is_a_flag_of_install_only_and_does_not_widen_a_file_that_is_clean(box, capsys):
    user_file(box, {"model": "opus"})
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    assert entry(box)["widened_by_user"] == []
    assert ws012(box)[0] == "ok"


def test_doctor_gate_check_does_not_call_a_recorded_choice_a_stale_entry(box, capsys):
    user_file(box, {"sandbox": {"enabled": False}})
    assert run_install(capsys, box, "--allow-widened-sandbox")[0] == 0
    status, detail, _ = userconfig.check()
    assert status == userconfig.OK, detail


# --- the other WS012 states --------------------------------------------------------------------------------------------


def test_ws012_says_unsandboxed_when_the_policy_switches_the_sandbox_off(box, capsys):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    status, detail, _ = ws012(box)
    assert status == "warn" and "unsandboxed" in detail


def test_ws012_says_no_block_and_names_the_way_to_fewer_refusals_when_the_file_has_none(box, capsys):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    f = box / ".claude" / "settings.json"
    conf = json.loads(f.read_text())
    del conf["sandbox"]
    f.write_text(json.dumps(conf))
    status, detail, fix = ws012(box)
    assert status == "warn" and "no sandbox block" in detail and "ws gate launch" in detail and "ws install" in fix


def test_ws012_reports_in_the_doctor_run(box, capsys):
    from ws import doctor, osinfo

    user_file(box)
    assert run_install(capsys, box)[0] == 0
    checks = doctor.run_checks(osinfo.detect(), install.profile_path(), dict(__import__("os").environ), network=False)
    line = next(c for c in checks if c.id == "WS012")
    assert line.status == "ok"
