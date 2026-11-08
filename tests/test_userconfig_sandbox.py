"""The sandbox on by default (0.1.8): the runner's own `sandbox` block is generated, and `ws install` merges it into the
user's Claude Code settings beside their own keys; the policy's `sandbox = false` takes it out again and the session is
then unsandboxed (strict reading, a doctor line). Everything runs under a tmp HOME and WS_HOME."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from ws import cli, paths, policy, userconfig, wallsettings
from ws.adapters import claude_code


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings(box) -> dict:
    return json.loads((box / ".claude" / "settings.json").read_text(encoding="utf-8"))


def install(capsys, box, *extra):
    code = cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes"), *extra])
    capsys.readouterr()
    return code


def generated(pol: policy.Policy) -> dict:
    files = claude_code.generate(pol, ["ws"], Path("out"), Path("wshome/policy.toml"))
    return json.loads(files["settings.json"])


def no_wall_flag(conf: dict) -> bool:
    """No hook command in the settings carries `--wall`: no mode turns a refusal into an allow."""
    cmds = [h["command"] for groups in conf["hooks"].values() for g in groups for h in g["hooks"]]
    return bool(cmds) and not any("--wall" in shlex.split(c) for c in cmds)


# --- what is generated --------------------------------------------------------------------------------------------------


def test_the_generated_settings_carry_the_sandbox_block_and_no_hook_changes_a_refusal():
    conf = generated(policy.default_policy())
    sb = conf["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert "~/.ssh" in sb["filesystem"]["denyRead"]
    assert any(p.endswith(("policy.toml", "wshome")) for p in sb["filesystem"]["denyWrite"])
    assert conf["env"]["WS_SANDBOXED"] == "1"
    assert no_wall_flag(conf)


def test_the_policy_switch_leaves_the_block_out_and_says_so_in_the_marker():
    conf = generated(policy.loads("[gate]\nsandbox = false\n"))
    assert "sandbox" not in conf and conf["env"]["WS_SANDBOXED"] == "0"
    assert no_wall_flag(conf)


def test_the_project_copy_of_unsandboxed_settings_is_the_settings_unchanged():
    text = claude_code.generate(policy.loads("[gate]\nsandbox = false\n"), ["ws"], Path("out"), Path("p"))[
        "settings.json"
    ]
    assert json.loads(claude_code.for_project(text, "/work/app")) == json.loads(text)


def test_the_policy_key_is_validated_and_documented_in_the_default_policy():
    assert policy.default_policy().gate.sandbox is True
    assert "sandbox = true" in policy.default_toml()
    with pytest.raises(policy.PolicyError, match="sandbox must be true or false"):
        policy.loads('[gate]\nsandbox = "yes"\n')


# --- ws install merges it into the user's file --------------------------------------------------------------------------


def test_install_writes_the_block_beside_the_users_own_keys_and_stops_marking_the_session_unsandboxed(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"model": "opus", "sandbox": {"filesystem": {"denyRead": ["~/mine"]}}}))
    assert install(capsys, box) == 0
    conf = settings(box)
    sb = conf["sandbox"]
    assert conf["model"] == "opus"
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["filesystem"]["denyRead"][0] == "~/mine" and "~/.ssh" in sb["filesystem"]["denyRead"]  # yours first
    assert any(p.endswith(".ws") for p in sb["filesystem"]["denyWrite"])  # ws's own folder is read-only to a shell
    assert conf["env"]["WS_SANDBOXED"] == "1"
    assert no_wall_flag(conf)


def test_the_off_switch_takes_the_block_out_and_marks_the_session_unsandboxed(box, capsys):
    assert install(capsys, box) == 0
    assert "sandbox" in settings(box)
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    assert install(capsys, box) == 0
    conf = settings(box)
    assert "sandbox" not in conf and conf["env"]["WS_SANDBOXED"] == "0"
    assert no_wall_flag(conf)


def test_turning_it_back_on_puts_the_block_back_and_a_second_install_changes_nothing(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    assert install(capsys, box) == 0 and "sandbox" not in settings(box)
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = true\n")
    assert install(capsys, box) == 0 and settings(box)["sandbox"]["enabled"] is True
    after = (box / ".claude" / "settings.json").read_bytes()
    assert install(capsys, box) == 0
    assert (box / ".claude" / "settings.json").read_bytes() == after


def test_a_user_who_has_set_the_sandbox_off_is_refused_naming_the_key_and_the_file_and_nothing_changes(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"sandbox": {"enabled": False}}))
    before = f.read_bytes()
    code = cli.main(["install", "--yes", "--json", "--runner", "claude-code", "--notes", str(box / "Notes")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error_kind"] == "refused"
    assert "sandbox.enabled" in out["message"] and str(f) in out["message"]
    assert "--allow-widened-sandbox" in out["hint"] and f.read_bytes() == before


def test_the_users_denyread_survives_an_uninstall_and_ws_items_come_out(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"sandbox": {"filesystem": {"denyRead": ["~/mine"]}}}))
    assert install(capsys, box) == 0
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    conf = json.loads(f.read_text()) if f.exists() else {}
    assert conf.get("sandbox", {}).get("filesystem", {}).get("denyRead", ["~/mine"]) == ["~/mine"]
    assert "enabled" not in conf.get("sandbox", {})


def test_the_hook_pins_and_the_block_use_the_same_protected_folders(box, capsys):
    assert install(capsys, box) == 0
    conf = settings(box)
    deny_write = conf["sandbox"]["filesystem"]["denyWrite"]
    pinned = [paths.pin_of().args()[i + 1] for i, a in enumerate(paths.pin_of().args()) if a == "--pin"]
    assert deny_write and pinned  # the pinned layout and the protected set come from the same install
    assert userconfig.check()[0] in (userconfig.OK, userconfig.WARN)


# --- bypass off: one value, in the launch settings, the user's file and the doctor rows ------------------------------


def test_bypass_off_is_in_user_and_launch_settings_and_comes_out_on_withdraw(box, capsys):
    assert wallsettings.BYPASS_KEY == "disableBypassPermissionsMode" and wallsettings.BYPASS_OFF == "disable"
    conf = generated(policy.default_policy())  # what ws gate generate writes: the launch settings are made from it
    assert conf["permissions"][wallsettings.BYPASS_KEY] == "disable"
    shared = claude_code.generate(policy.default_policy(), ["ws"], Path("out"), Path("p"), user_scope=True)
    assert json.loads(shared["settings.json"])["permissions"][wallsettings.BYPASS_KEY] == "disable"
    launch = json.loads(claude_code.for_project(json.dumps(conf), "/work/app"))
    assert launch["permissions"][wallsettings.BYPASS_KEY] == "disable"
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}}))
    assert install(capsys, box) == 0
    assert settings(box)["permissions"][wallsettings.BYPASS_KEY] == "disable"
    after = f.read_bytes()
    assert install(capsys, box) == 0 and f.read_bytes() == after  # a second run changes nothing
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    left = json.loads(f.read_text())
    assert wallsettings.BYPASS_KEY not in left["permissions"] and left["permissions"]["deny"] == ["Bash(curl:*)"]


def test_a_persons_other_bypass_value_is_a_conflict_and_their_own_disable_stays_theirs(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"permissions": {wallsettings.BYPASS_KEY: "off"}}))
    before = f.read_bytes()
    code = cli.main(["install", "--yes", "--json", "--runner", "claude-code", "--notes", str(box / "Notes")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error_kind"] == "conflict" and wallsettings.BYPASS_KEY in out["message"]
    assert f.read_bytes() == before
    f.write_text(json.dumps({"permissions": {wallsettings.BYPASS_KEY: "disable"}}))
    assert install(capsys, box) == 0
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    assert json.loads(f.read_text())["permissions"] == {wallsettings.BYPASS_KEY: "disable"}  # theirs, so it stays
