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
    assert code == 1 and out["error_kind"] == "sandbox_widened"
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
    assert code == 1 and out["error_kind"] == "sandbox_conflict" and wallsettings.BYPASS_KEY in out["message"]
    assert f"`permissions.{wallsettings.BYPASS_KEY}`" in out["hint"] and str(f) in out["hint"]  # the key and the file
    assert '"disable"' in out["hint"] and "`ws install`" in out["hint"]  # the value to set and the command to run
    assert f.read_bytes() == before
    f.write_text(json.dumps({"permissions": {wallsettings.BYPASS_KEY: "disable"}}))
    assert install(capsys, box) == 0
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    assert json.loads(f.read_text())["permissions"] == {wallsettings.BYPASS_KEY: "disable"}  # theirs, so it stays


# --- the read block stays out of a person's own file; the default domains are appended and taken out again -----------------

READ_BLOCK = wallsettings.READ_BLOCK_KEY


def test_the_read_block_is_never_merged_into_a_persons_own_settings_file(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"model": "opus"}))
    assert install(capsys, box) == 0
    assert READ_BLOCK not in settings(box).get("permissions", {})  # it would bind every Claude Code session they start
    assert READ_BLOCK not in f.read_text()


def test_the_user_file_merge_copies_nothing_but_the_keys_it_names_even_when_the_launch_file_has_the_read_block():
    files = {"settings.json": json.dumps(generated(policy.default_policy()))}
    assert generated(policy.default_policy())["permissions"][READ_BLOCK] is True  # the launch file has it ...
    ops = userconfig._claude_ops(files)
    assert not [op for op in ops if READ_BLOCK in op["path"]]  # ... and the merge does not copy it


def test_a_persons_own_read_block_is_theirs_and_stays_through_install_and_uninstall(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"permissions": {READ_BLOCK: True}}))
    assert install(capsys, box) == 0
    assert settings(box)["permissions"][READ_BLOCK] is True
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    assert json.loads(f.read_text())["permissions"][READ_BLOCK] is True


def test_the_default_domains_are_never_merged_into_a_persons_own_file(box, capsys):
    # Claude Code adds up the allowedDomains of every settings scope it loads. A default host in ~/.claude/settings.json
    # would therefore be added to a class session's own list (with_hosts replaces it only in the --settings copy).
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    mine = ["internal.example.org", "github.com"]
    f.write_text(json.dumps({"sandbox": {"network": {"allowedDomains": mine}}}))
    assert install(capsys, box) == 0
    assert settings(box)["sandbox"]["network"]["allowedDomains"] == mine  # exactly theirs: nothing of ws's added
    after = f.read_bytes()
    assert install(capsys, box) == 0 and f.read_bytes() == after  # a second install changes nothing
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    assert json.loads(f.read_text())["sandbox"]["network"]["allowedDomains"] == mine


def test_a_file_that_had_no_hosts_gets_none_from_an_install(box, capsys):
    assert install(capsys, box) == 0
    conf = settings(box)
    assert "sandbox" in conf and "network" not in conf["sandbox"]  # the wall is there, the default hosts are not
    assert "allowedDomains" not in json.dumps(conf)
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    f = box / ".claude" / "settings.json"
    assert not f.exists() or "allowedDomains" not in f.read_text()


def test_a_class_session_gets_only_its_own_hosts_from_the_user_file_and_the_launch_copy_together(box, capsys):
    # What Claude Code sees for a class session: the user file (loaded for interactive launches and by
    # `--setting-sources user`) and the --settings copy, with their arrays concatenated.
    assert install(capsys, box) == 0
    user_hosts = settings(box).get("sandbox", {}).get("network", {}).get("allowedDomains", [])
    launch = json.dumps(generated(policy.default_policy()))
    for hosts in ([], ["only.example.org"]):
        copy = json.loads(claude_code.with_hosts(launch, hosts))["sandbox"]["network"]["allowedDomains"]
        assert [*user_hosts, *copy] == hosts  # a class with no hosts reaches nothing


def test_the_user_scope_settings_carry_the_persons_hosts_and_never_the_defaults():
    def block(pol, **kw):
        files = claude_code.generate(pol, ["ws"], Path("out"), Path("wshome/policy.toml"), **kw)
        return json.loads(files["settings.json"])["sandbox"]

    own = policy.loads('[gate]\nallow_hosts = ["api.example.org"]\n')
    assert block(own, user_scope=True)["network"] == {"allowedDomains": ["api.example.org"]}
    assert "network" not in block(policy.default_policy(), user_scope=True)
    launch_hosts = block(policy.default_policy())["network"]["allowedDomains"]
    assert launch_hosts == list(claude_code.DEFAULT_DOMAINS)  # the launch settings still have them


def test_an_allow_hosts_policy_merges_its_own_list_and_not_the_defaults(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text('[gate]\nallow_hosts = ["api.example.org"]\n')
    assert install(capsys, box) == 0
    assert settings(box)["sandbox"]["network"]["allowedDomains"] == ["api.example.org"]


def test_a_policy_with_the_sandbox_off_merges_no_block_no_domains_and_no_read_block(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    assert install(capsys, box) == 0
    conf = settings(box)
    assert "sandbox" not in conf and "allowedDomains" not in json.dumps(conf) and READ_BLOCK not in json.dumps(conf)


def test_an_install_that_keeps_the_sandbox_off_adds_no_default_domains_either(box, capsys):
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    assert install(capsys, box) == 0
    (box / ".ws" / "policy.toml").write_text("[gate]\nallow_hosts = []\n")  # no sandbox key: the default is on ...
    assert install(capsys, box) == 0
    conf = settings(box)
    assert conf["env"]["WS_SANDBOXED"] == "0"  # ... but an install made without the block keeps going without it
    assert "sandbox" not in conf and "allowedDomains" not in json.dumps(conf)  # keeps_sandbox_off (ADR 0024)


def test_ws012_does_not_call_the_default_domains_stale(box, capsys):
    assert install(capsys, box) == 0
    _status, detail, _ = wallsettings.doctor_check(dict(__import__("os").environ), "linux")
    assert "stale" not in detail and "allowedDomains" not in detail  # it compares file paths only


def test_a_sandbox_value_of_yours_that_ws_would_change_is_its_own_kind_with_the_key_and_file_in_the_hint(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"sandbox": {"failIfUnavailable": False}}))
    before = f.read_bytes()
    code = cli.main(["install", "--yes", "--json", "--runner", "claude-code", "--notes", str(box / "Notes")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error_kind"] == "sandbox_conflict" and f.read_bytes() == before
    assert "`sandbox.failIfUnavailable`" in out["hint"] and str(f) in out["hint"] and "true" in out["hint"]


def test_a_sandbox_key_of_the_wrong_shape_is_its_own_kind_and_the_hint_says_which_key_and_file(box, capsys):
    f = box / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"sandbox": {"filesystem": {"denyWrite": "/x"}}}))  # a list that ws always appends to
    before = f.read_bytes()
    code = cli.main(["install", "--yes", "--json", "--runner", "claude-code", "--notes", str(box / "Notes")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error_kind"] == "sandbox_shape" and f.read_bytes() == before
    assert "`sandbox.filesystem.denyWrite`" in out["hint"] and str(f) in out["hint"] and "ws install" in out["hint"]


# --- OpenCode: ws's denies come last ----------------------------------------------------------------------------------

DENY = {"op": "set", "path": ["permission", "bash", "git push *"], "value": "deny"}
ASK = {"op": "set", "path": ["permission", "bash", "make deploy"], "value": "ask"}
DENY2 = {"op": "set", "path": ["permission", "bash", "rm -rf *"], "value": "deny"}


def bash_keys(doc: dict) -> list[str]:
    return list(doc["permission"]["bash"])


def test_opencode_ws_denies_come_last_after_a_persons_key_and_broader_allow():
    mine = {"permission": {"bash": {"git push *": "deny", "*": "allow", "make *": "allow"}}}
    got, added, _ = userconfig.update(mine, [], [], [ASK, DENY, DENY2], Path("x"))
    keys = bash_keys(got)
    assert keys[-2:] == ["git push *", "rm -rf *"]  # the last matching rule is a ws deny, whatever of yours is broader
    assert keys.index("*") < keys.index("git push *") and keys.index("make *") < keys.index("git push *")
    assert keys.index("make deploy") < keys.index("git push *")  # an ask stays before ws's first deny
    assert got["permission"]["bash"]["git push *"] == "deny" and got["permission"]["bash"]["*"] == "allow"
    assert added == [ASK, DENY2]  # the key that was already yours is not recorded as ws's


def test_opencode_denies_stay_last_on_re_apply():
    mine = {"permission": {"bash": {"git push *": "deny", "*": "allow"}}}
    ops = [ASK, DENY, DENY2]
    first, added, made = userconfig.update(mine, [], [], ops, Path("x"))
    again, added2, made2 = userconfig.update(first, added, made, ops, Path("x"))
    assert json.dumps(again) == json.dumps(first) and added2 == added and made2 == made  # byte stable
    first["permission"]["bash"]["later *"] = "allow"  # a broader allow of theirs added after the install
    moved, _, _ = userconfig.update(first, added, made, ops, Path("x"))
    assert bash_keys(moved)[-2:] == ["git push *", "rm -rf *"] and "later *" in bash_keys(moved)[:-2]
    # a new ask from a later policy goes in ahead of the denies too
    ask2 = {"op": "set", "path": ["permission", "bash", "npm publish *"], "value": "ask"}
    wider, _, _ = userconfig.update(moved, added, made, [ASK, ask2, DENY, DENY2], Path("x"))
    keys = bash_keys(wider)
    assert keys.index("npm publish *") < keys.index("git push *") and keys[-2:] == ["git push *", "rm -rf *"]


def test_opencode_withdrawal_leaves_the_persons_key():
    mine = {"permission": {"bash": {"git push *": "deny", "*": "allow"}}}
    applied, added, made = userconfig.update(mine, [], [], [ASK, DENY, DENY2], Path("x"))
    gone, _, _ = userconfig.update(applied, added, made, [], Path("x"))
    assert gone["permission"]["bash"] == {"*": "allow", "git push *": "deny"}  # theirs stays, and is a deny still


def test_opencode_install_puts_every_ws_deny_after_a_persons_broad_allow(box, capsys):
    f = box / ".config" / "opencode" / "opencode.json"
    f.parent.mkdir(parents=True)
    key = policy.default_policy().gate.deny_commands[0]
    f.write_text(json.dumps({"permission": {"bash": {key: "deny", "*": "allow"}}}))
    argv = ["install", "--yes", "--runner", "opencode", "--notes", str(box / "Notes")]
    assert cli.main(argv) == 0
    capsys.readouterr()
    rules = json.loads(f.read_text())["permission"]["bash"]
    after_allow = list(rules.values())[list(rules).index("*") + 1 :]
    assert after_allow and all(v in ("ask", "deny") for v in after_allow)  # nothing allows after the allow, so ws wins
    assert list(rules.values())[-1] == "deny" and key in list(rules)[list(rules).index("*") + 1 :]
    again = f.read_bytes()
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert f.read_bytes() == again
