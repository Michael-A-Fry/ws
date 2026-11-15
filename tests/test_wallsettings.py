"""The wall's settings: `ws install` refuses to merge beside settings that open the sandbox unless the person keeps them on
purpose (`--allow-widened-sandbox`, recorded), and `ws doctor` WS012 reports what the files say, never "ok" while
something widens the wall or the `denyRead` list is stale. Everything runs under a tmp HOME and WS_HOME; the managed
settings files are pointed at a tmp folder by tests/conftest.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws import cli, install, osinfo, policy, sandbox, userconfig, wallsettings

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
    assert code == 1 and out["error_kind"] == "sandbox_widened"
    assert (
        "sandbox.excludedCommands git:*" in out["message"] and "sandbox.filesystem.allowRead ~/.ssh" in out["message"]
    )
    assert str(f) in out["message"]  # the file, for each
    assert "--allow-widened-sandbox" in out["hint"] and "ws install" in out["hint"]
    assert "`sandbox.excludedCommands` from " + str(f) in out["hint"]  # what to delete, and from which file
    assert "`sandbox.filesystem.allowRead` from " + str(f) in out["hint"]
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
    assert code == 1 and out["error_kind"] == "sandbox_widened" and key in out["message"] and str(f) in out["message"]


@pytest.mark.parametrize(
    "doc",
    [
        {"sandbox": {"excludedCommands": []}},
        {"sandbox": {"enabled": True, "allowUnsandboxedCommands": False}},
        {"sandbox": {"filesystem": {"allowRead": ["/opt/tools"], "allowWrite": ["~/.cache/mytool"]}}},
        {"model": "opus"},
    ],
)
def test_settings_that_leave_the_wall_whole_are_not_refused(box, capsys, doc):
    user_file(box, doc)
    code, _ = run_install(capsys, box)
    assert code == 0
    assert ws012(box)[0] == "ok"


@pytest.mark.parametrize("entry", ["~/work", "$HOME/work/**", "{home}/work"])
def test_a_user_allow_read_at_or_under_the_home_is_a_widening_and_makes_ws012_not_ok(box, capsys, entry):
    """The launch settings hide the home and Claude Code merges this file into them, so the entry would re-open it."""
    entry = entry.format(home=box, parent=box.parent)
    f = user_file(box, {"sandbox": {"filesystem": {"allowRead": [entry]}}})
    code, out = run_install(capsys, box)
    assert code == 1 and out["error_kind"] == "sandbox_widened"
    assert f"sandbox.filesystem.allowRead {entry}" in out["message"] and "ws gate launch" in out["message"]
    code, _ = run_install(capsys, box, "--allow-widened-sandbox")
    assert code == 0
    status, detail, _ = ws012(box)
    assert status == "warn" and f"sandbox.filesystem.allowRead {entry}" in detail and str(f) in detail


def test_a_user_allow_read_outside_the_home_is_not_a_widening(box, capsys):
    user_file(box, {"sandbox": {"filesystem": {"allowRead": ["/opt/tools", "/usr/local/share/data"]}}})
    assert run_install(capsys, box)[0] == 0
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


# --- a class file in place: what a later user setting adds beyond it -----------------------------------------------------


def class_file_in_place(box, monkeypatch, tmp_path):
    """The policy gets a class with an OS user and the file ws renders for it sits in a managed-settings folder."""
    import os

    pol = install.policy.policy_path(dict(os.environ))
    pol.write_text(
        pol.read_text(encoding="utf-8")
        + '\n[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["/srv/work/**"]\n',
        encoding="utf-8",
    )
    drop = tmp_path / "managed-settings.d"
    drop.mkdir()
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(drop),))
    cls = install.policy.load(pol).classes["work"]
    file = drop / wallsettings.class_file_name("work")
    file.write_text(wallsettings.class_file_text(cls, str(box), "linux"), encoding="utf-8")
    return file


def test_ws012_reports_a_managed_file_widened_by_a_later_user_setting(box, capsys, monkeypatch, tmp_path):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    file = class_file_in_place(box, monkeypatch, tmp_path)
    assert ws012(box)[0] == "ok"  # the class file is ws's own wall, and nothing of the person's goes beyond it
    f = box / ".claude" / "settings.json"
    conf = json.loads(f.read_text())
    conf["sandbox"]["filesystem"]["allowWrite"] = ["/srv/work/sub", "/tmp/scratch"]  # inside the repos and scratch
    f.write_text(json.dumps(conf))
    assert ws012(box)[0] == "ok"
    conf["sandbox"]["filesystem"]["allowWrite"] += ["/srv/elsewhere"]
    conf["sandbox"]["filesystem"]["allowRead"] = ["~/notes"]
    conf["permissions"][wallsettings.BYPASS_KEY] = "off"
    f.write_text(json.dumps(conf))
    status, detail, _ = ws012(box)
    assert status == "warn" and detail.startswith("wall widened by user: ")
    assert "sandbox.filesystem.allowWrite /srv/elsewhere" in detail
    assert "sandbox.filesystem.allowRead ~/notes" in detail
    assert f"permissions.{wallsettings.BYPASS_KEY}" in detail and str(f) in detail
    assert "allowWrite /srv/work/sub" not in detail and str(file) not in detail  # the class file itself is not blamed
    file.unlink()  # without the class file, the allowWrite of the person's is no one's concern but their own
    assert "allowWrite /srv/elsewhere" not in ws012(box)[1]


def test_ws012_reads_a_class_file_that_differs_from_what_ws_renders_like_any_managed_file(
    box, capsys, monkeypatch, tmp_path
):
    user_file(box)
    assert run_install(capsys, box)[0] == 0
    file = class_file_in_place(box, monkeypatch, tmp_path)
    conf = json.loads(file.read_text())
    conf["sandbox"]["excludedCommands"] = ["npm:*"]
    file.write_text(json.dumps(conf))
    status, detail, _ = ws012(box)
    assert status == "warn" and "sandbox.excludedCommands npm:*" in detail and str(file) in detail


# --- permissions.blockReadsOutsideWorkingDirectories ---------------------------------------------------------------------


def launch_settings(box, toml: str = "") -> dict:
    """The settings ws generates with user_scope False: the file a launch and a headless run start from."""
    import os

    from ws import policy
    from ws.adapters import claude_code

    pol = policy.loads(toml)
    out = box / ".ws" / "gate" / "claude-code"
    files = claude_code.generate(pol, ["ws"], out, policy.policy_path(dict(os.environ)))
    return json.loads(files["settings.json"])


def test_the_launch_settings_and_the_launch_file_carry_the_read_block(box):
    assert launch_settings(box)["permissions"][wallsettings.READ_BLOCK_KEY] is True
    cls = wallsettings.policy.loads(
        '[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["/srv/work/**"]\n'
    ).classes["work"]
    assert wallsettings.launch_settings(cls, str(box), "linux")["permissions"][wallsettings.READ_BLOCK_KEY] is True


def test_launch_off_switch_leaves_the_key_out(box):
    perms = launch_settings(box, "[gate]\nread_outside_project = true\n")["permissions"]
    assert wallsettings.READ_BLOCK_KEY not in perms
    assert perms[wallsettings.BYPASS_KEY] == wallsettings.BYPASS_OFF  # only the read block went


def test_the_generate_command_writes_the_key_and_the_off_switch_removes_it(box, capsys):
    out = box / "gen"
    (box / ".ws").mkdir(exist_ok=True)
    (box / ".ws" / "policy.toml").write_text("")
    args = ["gate", "generate", "--runner", "claude-code", "--out", str(out)]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert json.loads((out / "settings.json").read_text())["permissions"][wallsettings.READ_BLOCK_KEY] is True
    (box / ".ws" / "policy.toml").write_text("[gate]\nread_outside_project = true\n")
    assert cli.main(args) == 0
    capsys.readouterr()
    assert wallsettings.READ_BLOCK_KEY not in json.loads((out / "settings.json").read_text())["permissions"]
    (box / ".ws" / "policy.toml").write_text('[gate]\nread_outside_project = "no"\n')
    assert cli.main(["--json", *args]) == 1  # a policy that does not load is refused, never partly used
    assert json.loads(capsys.readouterr().out)["error_kind"] == "bad_config"


def test_the_read_block_still_holds_with_the_sandbox_off(box):
    # it governs Claude's own file tools, which the sandbox never covered; only read_outside_project removes it
    perms = launch_settings(box, "[gate]\nsandbox = false\n")["permissions"]
    assert perms[wallsettings.READ_BLOCK_KEY] is True


@pytest.mark.parametrize("value", ['"yes"', "1", "[true]", '"true"'])
def test_read_outside_project_of_the_wrong_type_is_a_policy_error(value):
    from ws import policy

    with pytest.raises(policy.PolicyError, match="read_outside_project must be true or false"):
        policy.loads(f"[gate]\nread_outside_project = {value}\n")
    with pytest.raises(policy.PolicyError):  # never a partial load: the other keys do not rescue it
        policy.loads(f"[gate]\nsandbox = false\nread_outside_project = {value}\n")


def test_read_outside_project_is_off_by_default_and_true_is_read():
    from ws import policy

    assert policy.loads("").gate.read_outside_project is False
    assert policy.loads("[gate]\nread_outside_project = true\n").gate.read_outside_project is True


def class_rows_with(box, monkeypatch, tmp_path, capsys, edit=None):
    user_file(box)
    assert run_install(capsys, box)[0] == 0  # writes the policy the class goes into
    file = class_file_in_place(box, monkeypatch, tmp_path)
    if edit:
        conf = json.loads(file.read_text())
        edit(conf)
        file.write_text(json.dumps(conf))
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    pol = install.policy.load(install.policy.policy_path(dict(__import__("os").environ)))
    return {r.part: r for r in wallsettings.class_rows(pol, {"HOME": str(box)}, "linux")}


def test_ws014_reads_the_managed_settings_file_and_has_the_parts_it_had(box, monkeypatch, tmp_path, capsys):
    rows = class_rows_with(box, monkeypatch, tmp_path, capsys)
    assert list(rows) == list(wallsettings.PARTS) and len(wallsettings.PARTS) == 8  # no part for the launch file
    assert all(r.status == "ok" for p, r in rows.items() if p != "OS user exists"), rows
    managed = Path(wallsettings.MANAGED_DIRS[0]) / wallsettings.class_file_name("work")
    assert str(managed) in rows["file present and root-owned"].detail


def test_ws014_fails_a_managed_file_that_stops_requiring_the_sandbox(box, monkeypatch, tmp_path, capsys):
    rows = class_rows_with(box, monkeypatch, tmp_path, capsys, lambda c: c["sandbox"].update(enabled=False))
    assert rows["sandbox required"].status == "fail" and "ws install --class work" in rows["sandbox required"].fix


def test_the_widened_refusal_has_its_own_kind_and_a_hint_that_names_the_key_the_file_and_the_command(box):
    f = box / ".claude" / "settings.json"
    found = [wallsettings.Widening(("sandbox", "enabled"), "", f, "turns the sandbox off")]
    e = wallsettings.refusal(found, f)
    assert isinstance(e, wallsettings.WsError) and e.kind == "sandbox_widened"
    assert f"`sandbox.enabled` from {f}" in e.hint and "`ws install`" in e.hint and "--allow-widened-sandbox" in e.hint


def test_a_class_with_a_wildcard_repo_or_no_repo_folder_says_which_key_to_edit_and_what_to_run_again(box):
    from ws import policy

    head = '[classes.work]\naccess = "walled"\nos_user = "ws-work"\n'
    wild = policy.loads(head + 'repos = ["/srv/ops-*/x"]\n')
    bare = policy.loads(head + 'repos = ["checkout"]\n')
    for pol, call in (
        (wild, lambda c: wallsettings.class_roots(c, str(box))),
        (bare, lambda c: wallsettings.class_settings(c, str(box), "linux")),
    ):
        with pytest.raises(wallsettings.WsError) as e:
            call(pol.classes["work"])
        assert e.value.kind == "usage" and e.value.exit_code == 2
        assert "`repos` under [classes.work]" in e.value.hint and str(policy.policy_path()) in e.value.hint
        assert "`ws install --class work`" in e.value.hint


# --- the two files: the managed one as it was, and a launch file per class in a folder Claude Code does not read ---------

TWO = """
[classes.alpha]
access = "walled"
os_user = "ws-alpha"
allowed_providers = ["anthropic"]
repos = ["{box}/work/alpha/**"]

[classes.beta]
access = "walled"
os_user = "ws-beta"
allowed_providers = ["anthropic"]
repos = ["{box}/work/beta/**"]
"""


def two(box):
    return policy.loads(TWO.format(box=box))


def rendered(pol, name):
    """The managed-settings file's content for a class."""
    cls = pol.classes[name]
    return wallsettings.class_settings(cls, wallsettings.class_home(cls, "linux"), "linux")


def launch_rendered(pol, name, env=None):
    """The launch file's content for a class."""
    cls = pol.classes[name]
    return wallsettings.launch_settings(cls, wallsettings.class_home(cls, "linux"), "linux", pol, env)


def dotted(doc, at=""):
    for k, v in doc.items():
        yield f"{at}{k}"
        if isinstance(v, dict):
            yield from dotted(v, f"{at}{k}.")


def test_the_managed_settings_file_is_what_0_1_9_wrote_key_for_key(box):
    """The machine-wide file a bare `claude` run as the class account is bound by: the same keys as before the launch file."""
    conf = rendered(two(box), "alpha")
    assert conf == {
        "permissions": {"disableBypassPermissionsMode": "disable"},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": [f"{box}/work/alpha", "/tmp"],
                "denyRead": ["~/"],
                "allowRead": [f"{box}/work/alpha"],
            },
            "network": {
                "allowManagedDomainsOnly": True,
                "allowedDomains": ["api.anthropic.com", "platform.claude.com"],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
        },
        "env": {"WS_INBOUND": "lead-only"},
    }
    cls = two(box).classes["alpha"]
    assert (
        wallsettings.class_file_text(cls, wallsettings.class_home(cls, "linux"), "linux")
        == json.dumps(conf, indent=2) + "\n"
    )


def test_the_launch_file_holds_exactly_the_isolation_keys_and_no_managed_only_key(box):
    pol = two(box)
    conf = launch_rendered(pol, "alpha")
    assert set(conf) == {
        "permissions",
        "crossSessionInbound",
        "isolatePeerMachines",
        "disableAllHooks",
        "disableRemoteControl",
        "disableClaudeAiConnectors",
        "sandbox",
        "env",
    }
    assert conf["disableRemoteControl"] is True and conf["disableClaudeAiConnectors"] is True  # G11
    assert conf["crossSessionInbound"] == "refuse" and conf["isolatePeerMachines"] is True
    assert conf["disableAllHooks"] is False  # a project's true would switch the gate hook off; this outranks it
    assert set(conf["permissions"]) == {"deny", wallsettings.READ_BLOCK_KEY, wallsettings.WRITE_BLOCK_KEY}
    assert conf["permissions"][wallsettings.READ_BLOCK_KEY] is True
    assert conf["permissions"][wallsettings.WRITE_BLOCK_KEY] is True  # C360, in the launch file only
    assert not wallsettings.tokens_missing(conf)
    deny = conf["permissions"]["deny"]
    assert {"SendMessage", "ListAgents"} <= set(deny)
    for tool in ("Read", "Edit"):  # the home, ws's own folders and the other class's repos, for both tools
        assert {
            f"{tool}(~/**)",
            f"{tool}(~/.config/ws/**)",
            f"{tool}(~/.ws/**)",
            f"{tool}(/{box}/work/beta/**)",
        } <= set(deny)
    assert set(conf["sandbox"]) == {
        "filesystem",
        "network",
        "credentials",  # C360: the token deny
        *wallsettings.WEAKER_KEYS,
    }  # no `enabled`, no write list: those are the managed file's; the two weaker-sandbox switches are off
    assert all(conf["sandbox"][k] is False for k in wallsettings.WEAKER_KEYS)
    assert conf["sandbox"]["filesystem"] == {"denyRead": [f"{box}/work/beta"]}  # the other classes' roots only
    assert conf["sandbox"]["network"] == {
        "strictAllowlist": True,
        "allowedDomains": ["api.anthropic.com", "platform.claude.com"],
    }
    assert conf["env"] == {wallsettings.WALL_MARKER: "1", **wallsettings.OTEL_OFF}
    flat = json.dumps(conf)
    assert "allowManaged" not in flat and "allowManagedHooksOnly" not in flat and "bypass" not in flat.lower()


def test_neither_file_holds_a_key_claude_code_reads_only_from_managed_settings_but_the_managed_one_it_had(box):
    pol = two(box)
    leaves = {k.split(".")[-1] for k in dotted(launch_rendered(pol, "alpha"))}
    for managed_only in wallsettings.MANAGED_ONLY:
        assert managed_only.split(".")[-1] not in leaves
    assert len(wallsettings.MANAGED_ONLY) >= 3
    # the machine-wide file is as it was: its one managed-only key is the one it always had
    old = {k.split(".")[-1] for k in dotted(rendered(pol, "alpha"))}
    assert [m for m in wallsettings.MANAGED_ONLY if m.split(".")[-1] in old] == [
        "sandbox.network.allowManagedDomainsOnly"
    ]


def test_every_new_key_is_in_the_table_ws016_reads(box):
    assert wallsettings.MIN_CLAUDE["crossSessionInbound"] == (2, 1, 224)
    assert wallsettings.MIN_CLAUDE["isolatePeerMachines"] == (2, 1, 224)
    assert {"crossSessionInbound", "isolatePeerMachines"} <= set(wallsettings.keys_written(two(box)))
    assert not {"crossSessionInbound", "isolatePeerMachines"} & set(wallsettings.keys_written(policy.loads("")))


def test_the_managed_file_is_in_the_managed_folder_and_the_launch_file_beside_it_never_in_it(
    box, monkeypatch, tmp_path
):
    a = wallsettings.class_file_path("alpha", "linux")
    la = wallsettings.launch_file_path("alpha", "linux")
    real = ("/etc", "/Library")  # a test never reaches the real machine-wide folders (tests/conftest.py)
    assert not str(a).startswith(real) and not str(la).startswith(real)
    assert a.parent == Path(wallsettings.MANAGED_DIRS[0]) and a.name == "50-ws-class-alpha.json"  # where 0.1.9 had it
    assert la.parent == a.parent.parent / "ws-classes" and la.name == "alpha.json"
    managed = tmp_path / "elsewhere" / "managed-settings.d"
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(managed),))  # read at call time: both move with it
    assert wallsettings.class_file_path("alpha", "linux") == managed / "50-ws-class-alpha.json"
    b = wallsettings.launch_file_path("alpha", "linux")
    assert b.parent == managed.parent / "ws-classes" and b != la
    assert managed not in b.parents and not str(b).startswith("/etc")
    assert wallsettings.launch_file_path("beta", "linux") != b  # one file per class
    mac = wallsettings.class_dir("macos")
    assert mac == wallsettings.MANAGED_DIRS[-1] and wallsettings.launch_dir("macos") == str(
        Path(mac).parent / "ws-classes"
    )


def test_merge_lets_the_launch_keys_win_adds_to_lists_and_changes_neither_input():
    base = {
        "permissions": {"deny": ["Bash(x)"], "disableBypassPermissionsMode": "off"},
        "sandbox": {
            "filesystem": {"allowWrite": ["/home/p"], "denyRead": ["/a"]},
            "network": {"allowedDomains": ["h1"]},
        },
        "env": {"A": "1", "WS_INBOUND": "x"},
    }
    cls = {
        "permissions": {"deny": ["SendMessage", "Bash(x)"], "disableBypassPermissionsMode": "disable"},
        "sandbox": {"filesystem": {"denyRead": ["~/"]}, "network": {"allowedDomains": ["h2"]}},
        "env": {"WS_INBOUND": "lead-only"},
        "crossSessionInbound": "refuse",
        "disableAllHooks": False,
    }
    base["disableAllHooks"] = True
    before = json.dumps([base, cls])
    got = wallsettings.merge(base, cls)
    assert json.dumps([base, cls]) == before
    assert got["permissions"]["disableBypassPermissionsMode"] == "disable"  # the class wins
    assert got["permissions"]["deny"] == ["Bash(x)", "SendMessage"]  # added to, no duplicate
    assert got["sandbox"]["filesystem"]["allowWrite"] == ["/home/p"]  # the launch file sets no write list of its own
    assert got["sandbox"]["filesystem"]["denyRead"] == ["/a", "~/"]
    assert got["sandbox"]["network"]["allowedDomains"] == ["h1", "h2"]  # the plan's hosts stay beside the class's
    assert got["env"] == {"A": "1", "WS_INBOUND": "lead-only"} and got["crossSessionInbound"] == "refuse"
    assert got["disableAllHooks"] is False  # the class's false wins over a true


def place_class_file(name, pol, monkeypatch, edit=None, mode=0o644):
    """The launch file ws renders, placed where `launch_file_path` says; the test is not root, so root is stood in for."""
    conf = launch_rendered(pol, name)
    if edit:
        edit(conf)
    f = wallsettings.launch_file_path(name, osinfo.detect().kind)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(conf), encoding="utf-8")
    f.chmod(mode)
    f.parent.chmod(0o755)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    return f


def mark(name):
    """An edit that makes a launch file tell its class apart: a key only that class's file holds."""
    return lambda conf: conf["env"].update({f"WS_TEST_FILE_OF_{name.upper()}": "1"})


def denies(conf):
    return conf["sandbox"]["filesystem"]["denyRead"]


def test_class_alphas_file_is_absent_from_the_merged_settings_of_a_session_outside_alpha(box, monkeypatch):
    pol = two(box)
    place_class_file("alpha", pol, monkeypatch, mark("alpha"))
    place_class_file("beta", pol, monkeypatch, mark("beta"))
    base = {"sandbox": {"filesystem": {"allowWrite": ["/p"]}}, "env": {"WS_GATED_SESSION": "1"}}
    for name in ("alpha", "beta"):  # each session reads its own file, through the one loader
        cls = pol.classes[name]
        doc = wallsettings.load_launch_file(cls, osinfo.detect().kind)
        merged = wallsettings.merge(base, doc)
        other = "beta" if name == "alpha" else "alpha"
        assert merged["env"][f"WS_TEST_FILE_OF_{name.upper()}"] == "1"
        assert f"WS_TEST_FILE_OF_{other.upper()}" not in merged["env"]  # the other class's file is not read
        assert denies(merged) == [f"{box}/work/{other}"]  # the other class's repos are hidden, its own are not
    unclassified = wallsettings.merge(base, {})  # a session of no class is given no launch file at all
    assert unclassified == base and "crossSessionInbound" not in unclassified


# --- the launch: the launch file merged in, and a project that holds more than the allow-list refused before the runner starts


@pytest.fixture
def lbox(box, monkeypatch):
    """A policy with two walled classes, the shared settings made from it, and a project folder in each class."""
    from ws.adapters import claude_code

    wshome = box / ".ws"
    pol = two(box)
    wshome.mkdir(exist_ok=True)
    (wshome / "policy.toml").write_text(TWO.format(box=box), encoding="utf-8")
    out = wshome / "gate" / "claude-code"
    for rel, text in claude_code.generate(pol, ["ws"], out, wshome / "policy.toml").items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    for name in ("alpha", "beta", "free"):
        (box / "work" / name / "app").mkdir(parents=True)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append(cmd) or 0)
    return box, pol, started


def launch_as(box, monkeypatch, user, where, *extra):
    monkeypatch.setattr(sandbox, "real_user", lambda: user)
    monkeypatch.chdir(box / "work" / where / "app")
    return cli.main(["gate", "launch", "--runner", "claude-code", *extra, "--", "claude"])


def session_conf(started):
    return json.loads(Path(started[-1][started[-1].index("--settings") + 1]).read_text())


def test_gate_launch_merges_the_launch_file_into_the_copy_the_session_starts_with(lbox, monkeypatch):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch, mark("alpha"))
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0
    conf = session_conf(started)
    assert conf["crossSessionInbound"] == "refuse" and {"SendMessage", "ListAgents"} <= set(conf["permissions"]["deny"])
    assert conf["permissions"][wallsettings.READ_BLOCK_KEY] is True and conf["disableAllHooks"] is False
    assert f"{box}/work/beta" in denies(conf)  # the other class's repos are hidden from this session
    assert conf["env"][wallsettings.WALL_MARKER] == "1" and conf["env"]["OTEL_LOG_USER_PROMPTS"] == "0"
    assert conf["env"]["WS_TEST_FILE_OF_ALPHA"] == "1"
    assert conf["env"]["WS_GATED_SESSION"] == "1" and conf["hooks"]["PreToolUse"]  # the launch copy's own are kept
    assert (
        conf["sandbox"]["network"]["strictAllowlist"] is True
        and "api.anthropic.com" in conf["sandbox"]["network"]["allowedDomains"]
    )
    assert "allowManagedHooksOnly" not in json.dumps(conf)  # a managed-only key reaches no session


def test_a_session_of_another_class_is_given_no_launch_file_of_alphas(lbox, monkeypatch):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch, mark("alpha"))
    place_class_file("beta", pol, monkeypatch, mark("beta"))
    assert launch_as(box, monkeypatch, "ws-beta", "beta") == 0
    beta = session_conf(started)
    assert "WS_TEST_FILE_OF_ALPHA" not in beta["env"] and beta["env"]["WS_TEST_FILE_OF_BETA"] == "1"
    assert f"{box}/work/alpha" in denies(beta) and f"{box}/work/beta" not in denies(beta)


def test_a_launch_never_falls_back_to_another_class_file(lbox, monkeypatch, capsys):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch, mark("alpha"))  # beta's file is not in place
    assert launch_as(box, monkeypatch, "ws-beta", "beta") == 3 and started == []
    err = capsys.readouterr().err
    assert "'beta'" in err and "is not in place" in err and "`ws install --class beta`" in err


def test_a_folder_in_no_class_starts_with_no_launch_file_at_all(lbox, monkeypatch):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch, mark("alpha"))
    assert launch_as(box, monkeypatch, "anyone", "free", "--provider", "local") == 0
    conf = session_conf(started)
    assert wallsettings.WALL_MARKER not in conf["env"] and "WS_TEST_FILE_OF_ALPHA" not in conf["env"]
    assert "crossSessionInbound" not in conf


@pytest.mark.parametrize(
    ("chmod", "words"),
    [
        (None, "is not in place"),
        (0o666, "writable by group or others"),
    ],
)
def test_the_launch_is_refused_when_the_launch_file_is_missing_or_unsafe(lbox, monkeypatch, capsys, chmod, words):
    box, pol, started = lbox
    if chmod:
        place_class_file("alpha", pol, monkeypatch, mode=chmod)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3
    err = capsys.readouterr().err
    assert words in err and "alpha" in err and started == []  # nothing started
    if not chmod:
        assert "`ws install --class alpha`" in err  # the typed error names what prints the set-up


def test_a_launch_file_that_root_does_not_own_is_refused_at_launch(lbox, monkeypatch, capsys):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: False)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3
    assert "not owned by root" in capsys.readouterr().err and started == []


def test_a_forged_user_or_home_or_ws_variable_never_chooses_the_class_or_the_file(lbox, monkeypatch, capsys):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    place_class_file("beta", pol, monkeypatch)
    for name, value in (
        ("USER", "ws-alpha"),
        ("LOGNAME", "ws-alpha"),
        ("WS_CLASS", "beta"),
        ("WS_CLASS_FILE", "/x/beta.json"),
    ):
        monkeypatch.setenv(name, value)
    # the account is beta's, the folder is alpha's: the class is the folder's and the user the system's, so refused
    assert launch_as(box, monkeypatch, "ws-beta", "alpha") == 3 and started == []
    here = wallsettings.launch_file_path("alpha", osinfo.detect().kind)
    monkeypatch.setenv("WS_CLASS_FILE", "/x/other.json")
    assert wallsettings.launch_file_path("alpha", osinfo.detect().kind) == here  # nothing in env moves the place
    capsys.readouterr()


REFUSED = [
    (".mcp.json", {"mcpServers": {"x": {"command": "run"}}}, "mcpServers.x"),
    (".claude/settings.json", {"hooks": {"PreToolUse": [{"hooks": []}]}}, "hooks"),
    (".claude/settings.local.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}, "hooks"),
    (".claude/settings.json", {"mcpServers": {"y": {}}}, "mcpServers"),
    (".claude/settings.json", {"disableAllHooks": True}, "disableAllHooks"),
    (
        ".claude/settings.local.json",
        {"disableAllHooks": False},
        "disableAllHooks",
    ),  # not on the list, whatever its value
    (".claude/settings.json", {"enabledPlugins": {"p@m": True}}, "enabledPlugins"),
    (".claude/settings.json", {"enabledPlugins": {"p@m": False}}, "enabledPlugins"),
    (
        ".claude/settings.json",
        {"extraKnownMarketplaces": {"m": {"source": {"source": "github"}}}},
        "extraKnownMarketplaces",
    ),
    (".claude/settings.json", {"fileSuggestion": {"type": "command", "command": "x"}}, "fileSuggestion"),
    (".claude/settings.json", {"enableAllProjectMcpServers": True}, "enableAllProjectMcpServers"),
    (".claude/settings.json", {"permissions": {"allow": ["Bash(*)"]}}, "permissions.allow"),
    (".claude/settings.json", {"permissions": {"additionalDirectories": ["/"]}}, "permissions.additionalDirectories"),
    (".claude/settings.json", {"permissions": {"defaultMode": "bypassPermissions"}}, "permissions.defaultMode"),
    (".claude/settings.json", {"permissions": {"defaultMode": "acceptEdits"}}, "permissions.defaultMode"),
    (".claude/settings.json", {"permissions": {"defaultMode": "auto"}}, "permissions.defaultMode"),
    (".claude/settings.json", {"statusLine": {"type": "command", "command": "x"}}, "statusLine"),
    (".claude/settings.json", {"apiKeyHelper": "x"}, "apiKeyHelper"),
    (".claude/settings.json", {"env": {"OTEL_LOG_USER_PROMPTS": "1"}}, "env.OTEL_LOG_USER_PROMPTS"),
    (".claude/settings.json", {"env": {"WS_WALLED_CLASS": "0"}}, "env.WS_WALLED_CLASS"),
    (".claude/settings.json", {"env": {"ANTHROPIC_BASE_URL": "http://x"}}, "env.ANTHROPIC_BASE_URL"),
    (".claude/settings.json", {"env": {"ANTHROPIC_FOO_BASE_URL": "http://x"}}, "env.ANTHROPIC_FOO_BASE_URL"),
    (".claude/settings.json", {"env": {"BASH_ENV": "/x"}}, "env.BASH_ENV"),
    (".claude/settings.json", {"env": {"LD_PRELOAD": "/x.so"}}, "env.LD_PRELOAD"),
    (".claude/settings.json", {"env": {"NODE_EXTRA_CA_CERTS": "/x.pem"}}, "env.NODE_EXTRA_CA_CERTS"),
    (
        ".claude/settings.json",
        {"env": {"TZ": "UTC", "PATH": "/x"}},
        "env.PATH",
    ),  # the first name is fine, the second not
    (".claude/settings.json", {"sandbox": {"enabled": False}}, "sandbox.enabled"),
    (".claude/settings.json", {"sandbox": {"network": {"allowedDomains": ["h"]}}}, "sandbox.network.allowedDomains"),
    # a project's sandbox table may only narrow: each of these is refused, by name or by value
    (".claude/settings.json", {"sandbox": {"enableWeakerNestedSandbox": True}}, "sandbox.enableWeakerNestedSandbox"),
    (".claude/settings.json", {"sandbox": {"enableWeakerNestedSandbox": False}}, "sandbox.enableWeakerNestedSandbox"),
    (
        ".claude/settings.json",
        {"sandbox": {"enableWeakerNetworkIsolation": True}},
        "sandbox.enableWeakerNetworkIsolation",
    ),
    (
        ".claude/settings.local.json",
        {"sandbox": {"enableWeakerNetworkIsolation": 1}},
        "sandbox.enableWeakerNetworkIsolation",
    ),
    (".claude/settings.json", {"sandbox": {"allowNestedUserNamespaces": True}}, "sandbox.allowNestedUserNamespaces"),
    (".claude/settings.json", {"sandbox": {"ignoreViolations": {"*": ["/x"]}}}, "sandbox.ignoreViolations"),
    (".claude/settings.json", {"sandbox": {"excludedCommands": []}}, "sandbox.excludedCommands"),
    (".claude/settings.json", {"sandbox": {"excludedCommands": ["git"]}}, "sandbox.excludedCommands"),
    (".claude/settings.json", {"sandbox": {"allowUnsandboxedCommands": True}}, "sandbox.allowUnsandboxedCommands"),
    (".claude/settings.json", {"sandbox": {"failIfUnavailable": False}}, "sandbox.failIfUnavailable"),
    (".claude/settings.json", {"sandbox": {"autoAllowBashIfSandboxed": True}}, "sandbox.autoAllowBashIfSandboxed"),
    (".claude/settings.json", {"sandbox": {"filesystem": {"allowRead": ["/x"]}}}, "sandbox.filesystem.allowRead"),
    (".claude/settings.json", {"sandbox": {"filesystem": {"allowWrite": ["/x"]}}}, "sandbox.filesystem.allowWrite"),
    (".claude/settings.json", {"sandbox": {"filesystem": {"disabled": True}}}, "sandbox.filesystem.disabled"),
    (".claude/settings.json", {"sandbox": {"filesystem": {"denyRead": "/x"}}}, "sandbox.filesystem.denyRead"),
    (".claude/settings.json", {"sandbox": {"filesystem": {"denyWrite": [1]}}}, "sandbox.filesystem.denyWrite"),
    (
        ".claude/settings.json",
        {"sandbox": {"network": {"allowUnixSockets": ["/s"]}}},
        "sandbox.network.allowUnixSockets",
    ),
    (
        ".claude/settings.json",
        {"sandbox": {"network": {"allowAllUnixSockets": False}}},
        "sandbox.network.allowAllUnixSockets",
    ),
    (".claude/settings.json", {"sandbox": {"network": {"httpProxyPort": 8080}}}, "sandbox.network.httpProxyPort"),
    (
        ".claude/settings.json",
        {"sandbox": {"credentials": {"files": [{"path": "/x", "mode": "mask"}]}}},
        "sandbox.credentials.files",
    ),
    (
        ".claude/settings.json",
        {"sandbox": {"credentials": {"allowPlaintextInject": True}}},
        "sandbox.credentials.allowPlaintextInject",
    ),
    (".claude/settings.json", {"sandbox": {"someKeyNextYear": True}}, "sandbox.someKeyNextYear"),
    (".claude/settings.json", {"sandbox": "off"}, "sandbox"),
]


@pytest.mark.parametrize(("rel", "doc", "key"), REFUSED)
def test_the_launch_is_refused_for_a_project_that_holds_what_the_allow_list_does_not_name(
    lbox, monkeypatch, capsys, rel, doc, key
):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    f = box / "work" / "alpha" / "app" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc))
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3
    err = capsys.readouterr().err
    assert str(f) in err and key in err and "hint:" in err and started == []
    refused = wallsettings.project_problem([str(box / "work" / "alpha" / "app")], box / "nohome")
    assert refused.kind == "project_load_refused" and refused.exit_code == 3 and str(f) in refused.hint
    assert f"`{key}`" in refused.hint or rel == ".mcp.json"  # a settings file: the hint names the key to delete


STARTS = [
    {"$schema": "https://json.schemastore.org/claude-code-settings.json", "permissions": {"deny": ["Bash(rm *)"]}},
    {"outputStyle": "Explanatory"},
    {"model": "x", "language": "english", "attribution": {"commit": ""}, "includeCoAuthoredBy": False},
    {"cleanupPeriodDays": 7, "respectGitignore": True},
    {"permissions": {"ask": ["Bash(git push *)"], "defaultMode": "plan"}},
    {"permissions": {"defaultMode": "default"}},
    {"env": dict.fromkeys(wallsettings.PROJECT_ENV, "1")},
    {"sandbox": {"enabled": True, "filesystem": {"denyRead": ["/srv/x"]}}},
    {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "filesystem": {"denyRead": ["/srv/x"], "denyWrite": ["/srv/y"]},
            "network": {"deniedDomains": ["bad.example"]},
            "credentials": {
                "files": [{"path": "~/.aws/credentials", "mode": "deny"}],
                "envVars": [{"name": "TOKEN", "mode": "deny"}],
            },
        }
    },
]


@pytest.mark.parametrize("doc", STARTS)
def test_a_project_that_holds_only_what_the_allow_list_names_starts(lbox, monkeypatch, doc):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    app = box / "work" / "alpha" / "app"
    (app / ".claude").mkdir(exist_ok=True)
    (app / ".claude" / "settings.json").write_text(json.dumps(doc))
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0 and started


def plant(box, rel: str, content: str | bytes, where: str = "work/alpha/app") -> Path:
    f = box / where / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return f


def head(*lines: str) -> str:
    return "---\n" + "".join(f"{ln}\n" for ln in lines) + "---\nbody\n"


# a skill, agent or command header that runs code or allows a call by itself, and a workflow: refused, with the file and key named
FRONT_REFUSED = [
    (".claude/skills/s/SKILL.md", head("name: s", "hooks:", "  PreToolUse: []"), "hooks"),
    (".claude/skills/a/b/c/SKILL.md", head("name: deep", "hooks: {}"), "hooks"),
    (".claude/skills/s/SKILL.md", head("name: s", "allowed-tools: Bash(*)"), "allowed-tools"),
    (".claude/skills/s/SKILL.md", head("name: s", "Allowed_Tools: [Bash]"), "Allowed_Tools"),
    (".claude/agents/a.md", head("name: a", "hooks:", "  Stop: []"), "hooks"),
    (".claude/agents/a.md", head("name: a", "mcpServers:", "  x: {command: y}"), "mcpServers"),
    (".claude/agents/a.md", head("name: a", "permissionMode: bypassPermissions"), "permissionMode"),
    (".claude/agents/team/a.md", head("name: a", "permissionMode: acceptEdits"), "permissionMode"),
    (".claude/commands/c.md", head("description: c", "hooks: {}"), "hooks"),
    (".claude/commands/x/y/c.md", head("allowed-tools: Bash(git *)"), "allowed-tools"),
    (".claude/workflows/w.js", "export default async () => {}\n", "workflow"),
    (".claude/workflows/w.JS", "x\n", "workflow"),
]


@pytest.mark.parametrize(("rel", "text", "key"), FRONT_REFUSED)
def test_the_launch_is_refused_for_a_skill_agent_command_or_workflow_that_runs_code(
    lbox, monkeypatch, capsys, rel, text, key
):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    f = plant(box, rel, text)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3
    err = capsys.readouterr().err
    assert str(f) in err and key in err and "hint:" in err and started == []
    refused = wallsettings.project_problem([str(box / "work" / "alpha" / "app")], box / "nohome")
    assert refused.kind == "project_load_refused" and refused.exit_code == 3 and str(f) in refused.hint
    assert refused.details["file"] == str(f)


def test_a_skill_whose_header_is_plain_and_whose_body_has_a_command_line_starts(lbox, monkeypatch):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    plant(
        box,
        ".claude/skills/s/SKILL.md",
        head(
            "name: s",
            "description: >",
            "  Use for hooks: it says hooks and allowed-tools here, under a key, not as keys",
            "# a comment",
            "",
            "tags:",
            "- hooks",
            "- mcpServers",
        )
        + "!`git status`\n---\nhooks: not a key either, the header closed above\n",
    )
    plant(box, ".claude/commands/plain.md", "no header at all\n")
    plant(box, ".claude/agents/empty.md", "")
    plant(box, ".claude/agents/ok.md", head("name: ok", "tools: Read, Grep", "model: sonnet"))
    plant(box, ".claude/skills/notes.txt", "hooks:\n")  # not a file Claude Code loads as a skill
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0 and started


def test_a_skill_in_a_folder_above_the_project_is_read_too(lbox, monkeypatch, capsys):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    f = plant(box, ".claude/skills/s/SKILL.md", head("hooks: {}"), "work/alpha")  # the folder above the project
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3 and started == []
    assert str(f) in capsys.readouterr().err


def test_the_hook_runs_the_frontmatter_rule_in_a_walled_session(box, monkeypatch):
    from ws.adapters import claude_code

    proj = box / "work" / "alpha" / "app"
    f = plant(box, ".claude/agents/a.md", head("permissionMode: bypassPermissions"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv(wallsettings.WALL_MARKER, raising=False)
    assert claude_code.preflight_refusal({"cwd": str(proj)}) is None  # not a walled session
    monkeypatch.setenv(wallsettings.WALL_MARKER, "1")
    refused = claude_code.preflight_refusal({"cwd": str(proj)})
    assert refused.kind == "project_load_refused" and str(f) in refused.hint and "permissionMode" in refused.message


# a file that exists but does not read as the runner would read it is a refusal that names it, never a skip
UNREADABLE = [
    (".mcp.json", "{ not json", "does not parse"),
    (".claude/settings.json", '{"hooks": {},}', "does not parse"),
    (".claude/settings.json", "", "does not parse"),
    (".claude/settings.json", "{}", "empty object"),
    (".claude/settings.local.json", "   \n", "does not parse"),
    (".claude/settings.json", "[]", "not a JSON object"),
    (".claude/settings.json", '"x"', "not a JSON object"),
    (".claude/settings.json", "null", "not a JSON object"),
    (".mcp.json", "[1]", "not a JSON object"),
    (".claude/settings.json", b"\xff\xfe{}", "not a plain readable file"),
    (".claude/settings.json", "[" * 100000, "does not parse"),
    (".claude/skills/s/SKILL.md", "---\nname: s\nhooks: {}\n", "no closing"),
    (".claude/skills/s/SKILL.md", "---\nname: s\n--- \nbody\n", "plain `key: value`"),
    (".claude/skills/s/SKILL.md", "---\r\nname: s\r\n---\r\nbody\r\n", "bare `---`"),
    (".claude/skills/s/SKILL.md", "---\nname: s\r\nhooks: {}\r\n---\n", "carriage return"),
    (".claude/skills/s/SKILL.md", '---\nname: s\n"hooks": {}\n---\n', "quoted key"),
    (".claude/skills/s/SKILL.md", "---\nname: s\n'hooks': {}\n---\n", "quoted key"),
    (".claude/skills/s/SKILL.md", "---\nname: s\nhooks : {}\n---\n", "plain `key: value`"),
    (".claude/skills/s/SKILL.md", "---\n{hooks: {}}\n---\n", "plain `key: value`"),
    (".claude/skills/s/SKILL.md", "---\nname: s\n? hooks\n---\n", "plain `key: value`"),
    (".claude/skills/s/SKILL.md", "---\nname: s\nthis is odd\n---\n", "plain `key: value`"),
    (".claude/skills/s/SKILL.md", "---\n  hooks: {}\n---\n", "indented under no key"),
    (".claude/skills/s/SKILL.md", "\n---\nname: s\n---\n", "first line"),
    (".claude/commands/c.md", "---\n", "no closing"),
    (".claude/agents/a.md", b"---\nname: \xff\n---\n", "not a plain readable file"),
]


@pytest.mark.parametrize(("rel", "content", "words"), UNREADABLE)
def test_a_project_file_that_exists_but_does_not_read_is_refused_naming_the_file(
    lbox, monkeypatch, capsys, rel, content, words
):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    f = plant(box, rel, content)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3 and started == []
    assert str(f) in capsys.readouterr().err
    refused = wallsettings.project_problem([str(box / "work" / "alpha" / "app")], box / "nohome")
    assert refused.kind == "project_settings_unreadable" and refused.exit_code == 3
    assert refused.details == {"file": str(f)} and words in refused.message and str(f) in refused.message


def test_a_byte_order_mark_does_not_hide_a_key_and_an_absent_file_is_fine(box):
    app = box / "work" / "alpha" / "app"
    app.mkdir(parents=True)
    nohome = box / "nohome"
    assert wallsettings.project_problem([str(app)], nohome) is None  # nothing there at all
    f = plant(box, ".claude/settings.json", b"\xef\xbb\xbf" + json.dumps({"hooks": {}}).encode())
    refused = wallsettings.project_problem([str(app)], nohome)
    assert refused.kind == "project_load_refused" and refused.details == {"file": str(f), "key": "hooks"}
    f.write_bytes(b"\xef\xbb\xbf{}")  # a BOM then an empty object: read as the runner reads it, so empty
    assert "empty object" in wallsettings.project_problem([str(app)], nohome).message
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "x"}).encode())
    assert wallsettings.project_problem([str(app)], nohome) is None
    g = plant(box, ".claude/skills/s/SKILL.md", b"\xef\xbb\xbf" + head("hooks: {}").encode())
    assert wallsettings.project_problem([str(app)], nohome).details["file"] == str(g)  # a BOM before the first fence


def test_an_empty_mcp_json_object_is_fine(box):
    app = box / "work" / "alpha" / "app"
    plant(box, ".mcp.json", "{}")
    assert wallsettings.project_problem([str(app)], box / "nohome") is None


def test_a_link_in_the_project_tree_is_refused_not_followed(box):
    app = box / "work" / "alpha" / "app"
    nohome = box / "nohome"
    elsewhere = box / "elsewhere"
    plant(elsewhere, "s/SKILL.md", head("hooks: {}"), ".")
    (app / ".claude").mkdir(parents=True)
    (app / ".claude" / "skills").symlink_to(elsewhere)  # the folder is a link
    refused = wallsettings.project_problem([str(app)], nohome)
    assert refused.kind == "project_settings_unreadable" and str(app / ".claude" / "skills") in refused.message
    (app / ".claude" / "skills").unlink()
    (app / ".claude" / "commands").mkdir()
    (app / ".claude" / "commands" / "linked").symlink_to(elsewhere)  # a folder inside it is a link
    assert "is a link" in wallsettings.project_problem([str(app)], nohome).message
    (app / ".claude" / "commands" / "linked").unlink()
    (app / ".claude" / "commands" / "c.md").symlink_to(elsewhere / "s" / "SKILL.md")  # a file it would load
    assert "is a link" in wallsettings.project_problem([str(app)], nohome).message
    (app / ".claude" / "commands" / "c.md").unlink()
    assert wallsettings.project_problem([str(app)], nohome) is None
    real = box / "realclaude"
    real.mkdir()
    (box / "work" / "beta" / "app").mkdir(parents=True)
    (box / "work" / "beta" / "app" / ".claude").symlink_to(real)  # `.claude` itself is a link
    refused = wallsettings.project_problem([str(box / "work" / "beta" / "app")], nohome)
    assert refused.kind == "project_settings_unreadable" and "is a link" in refused.message


def test_a_project_with_too_many_files_is_refused_not_skipped(box, monkeypatch):
    app = box / "work" / "alpha" / "app"
    for n in range(5):
        plant(box, f".claude/commands/c{n}.md", "x\n")
    assert wallsettings.project_problem([str(app)], box / "nohome") is None
    monkeypatch.setattr(wallsettings, "_MAX_FILES", 3)
    assert "too many files" in wallsettings.project_problem([str(app)], box / "nohome").message


def worktree_of(box, main: Path, wt: Path, gitdir_text: str | None = None) -> None:
    """`wt` as a git worktree of the repository at `main`, laid out as `git worktree add` lays it out."""
    admin = main / ".git" / "worktrees" / wt.name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(gitdir_text or f"gitdir: {admin}\n")


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        (".claude/settings.local.json", json.dumps({"hooks": {}})),
        (".claude/settings.json", json.dumps({"enabledPlugins": {"p@m": True}})),
        (".claude/skills/s/SKILL.md", head("hooks: {}")),
        (".mcp.json", json.dumps({"mcpServers": {"x": {}}})),
    ],
)
def test_a_worktrees_main_checkout_is_read_too(box, rel, content):
    main = box / "elsewhere" / "main"
    wt = box / "work" / "alpha" / "app"
    worktree_of(box, main, wt)
    assert wallsettings.project_problem([str(wt)], box / "nohome") is None  # a clean main checkout
    f = plant(box, rel, content, "elsewhere/main")
    refused = wallsettings.project_problem([str(wt)], box / "nohome")
    assert refused.kind == "project_load_refused" and refused.details["file"] == str(f)


def test_a_worktree_link_is_found_by_a_relative_path_and_a_plain_folder_has_none(box):
    main = box / "elsewhere" / "main"
    wt = box / "work" / "alpha" / "app"
    worktree_of(box, main, wt, "gitdir: ../../../elsewhere/main/.git/worktrees/app\n")
    assert wallsettings._main_checkout(wt) == main
    assert wallsettings._main_checkout(main) is None  # `.git` is a folder there
    assert wallsettings._main_checkout(box / "nothing") is None
    (wt / ".git").write_text("not a gitdir line\n")
    assert wallsettings._main_checkout(wt) is None
    (wt / ".git").write_text(f"gitdir: {box}/missing\n")
    assert wallsettings._main_checkout(wt) is None  # a submodule or an odd file: no commondir


def test_a_git_file_that_cannot_be_read_is_refused(box):
    wt = box / "work" / "alpha" / "app"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("x" * 5000)  # more than a `.git` file can be
    refused = wallsettings.project_problem([str(wt)], box / "nohome")
    assert refused.kind == "project_settings_unreadable" and refused.exit_code == 3


def test_the_launch_file_holds_the_two_weaker_sandbox_switches_off_and_a_project_may_not_set_them(box):
    pol = two(box)
    for name in ("alpha", "beta"):
        sb = launch_rendered(pol, name)["sandbox"]
        assert sb["enableWeakerNestedSandbox"] is False and sb["enableWeakerNetworkIsolation"] is False
    assert wallsettings.WEAKER_KEYS == ("enableWeakerNestedSandbox", "enableWeakerNetworkIsolation")
    for key in wallsettings.WEAKER_KEYS:
        for value in (True, False):
            assert wallsettings._adds(".claude/settings.json", {"sandbox": {key: value}})[0] == f"sandbox.{key}"


def test_the_allow_list_is_the_one_rule_and_names_no_deny_for_a_key_it_does_not_list():
    src = Path(wallsettings.__file__).read_text(encoding="utf-8")
    for dead in ("_COMMAND_KEYS", "_ENV_PREFIXES", "_ENV_NAMES"):  # the deny branches the allow-list replaced
        assert dead not in src
    assert wallsettings.PROJECT_ENV == tuple(dict.fromkeys(wallsettings.PROJECT_ENV))
    assert not any(n.startswith(("WS_", "OTEL_")) or n.endswith("_URL") for n in wallsettings.PROJECT_ENV)


def test_a_project_settings_file_that_is_a_link_is_refused_not_followed(lbox, monkeypatch, capsys):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    (box / "elsewhere.json").write_text("{}")
    (box / "work" / "alpha" / "app" / ".mcp.json").symlink_to(box / "elsewhere.json")
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3
    assert "not a plain readable file" in capsys.readouterr().err and started == []


def test_the_hook_runs_the_same_rule_on_every_call_in_a_walled_session_only(box, monkeypatch):
    from ws.adapters import claude_code

    proj = box / "work" / "alpha" / "app"
    proj.mkdir(parents=True)
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {}}}))
    payload = {"cwd": str(proj)}
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv(wallsettings.WALL_MARKER, raising=False)
    assert claude_code.preflight_refusal(payload) is None  # not a walled session: a project may name its own servers
    monkeypatch.setenv(wallsettings.WALL_MARKER, "1")  # the class file's env, which the session's --settings carries
    refused = claude_code.preflight_refusal(payload)
    assert refused.kind == "project_load_refused" and str(proj / ".mcp.json") in refused.hint
    same = wallsettings.project_problem([str(proj)], claude_code.paths.home_dir())
    assert (same.kind, same.message) == (refused.kind, refused.message)  # one rule, two callers
    assert "holds mcpServers.x" in claude_code.preflight(payload)
    (
        proj / ".mcp.json"
    ).unlink()  # a file that appears after the start is caught by the next call, and one removed is not
    assert claude_code.preflight_refusal(payload) is None


def test_per_repo_settings_are_applied_to_the_launch_copy_and_only_add_denials(lbox, monkeypatch):
    box, _, started = lbox
    text = TWO.format(box=box) + (
        f'\n[classes.alpha.repo_settings."{box}/work/alpha/**"]\n'
        'deny = ["Bash(curl *)"]\ndeny_read = ["/srv/secret"]\ndeny_write = ["/srv/frozen"]\n'
    )
    (box / ".ws" / "policy.toml").write_text(text, encoding="utf-8")
    place_class_file("alpha", policy.loads(text), monkeypatch)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0
    conf = session_conf(started)
    assert "Bash(curl *)" in conf["permissions"]["deny"] and "SendMessage" in conf["permissions"]["deny"]
    fs = conf["sandbox"]["filesystem"]
    assert "/srv/secret" in fs["denyRead"] and "/srv/frozen" in fs["denyWrite"]
    assert (
        not conf["permissions"].get("allow") and conf["permissions"].get("defaultMode") != "bypassPermissions"
    )  # only denials


# --- WS014 reads the managed-settings file; the launch file is `ws walls check`'s ---------------------------------------


def test_ws014_reads_the_managed_file_and_a_launch_file_alone_is_no_managed_file(box, monkeypatch):
    pol = two(box)
    place_class_file("alpha", pol, monkeypatch)  # a launch file in place, no managed-settings file
    rows = {(r.cls, r.part): r for r in wallsettings.class_rows(pol, {"HOME": str(box)}, "linux")}
    row = rows[("alpha", "file present and root-owned")]
    assert row.status == "warn" and str(wallsettings.class_file_path("alpha", "linux")) in row.detail
    assert "managed-settings file" in row.detail and "ws install --class alpha" in row.fix
    assert {r.part for r in rows.values()} == set(
        wallsettings.PARTS
    )  # no row for the launch file, none for an old place


def test_ws015_reads_the_managed_files_network_list(box, monkeypatch):
    pol = two(box)
    conf = rendered(pol, "alpha")
    conf["sandbox"]["network"]["allowedDomains"] = ["stale.example.com"]
    f = wallsettings.class_file_path("alpha", "linux")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(conf), encoding="utf-8")
    assert wallsettings.stale_networks(pol, {"HOME": str(box)}, "linux") == ["alpha"]
    f.write_text(json.dumps(rendered(pol, "alpha")), encoding="utf-8")
    assert wallsettings.stale_networks(pol, {"HOME": str(box)}, "linux") == []


def test_a_class_with_its_repos_under_its_own_home_seals_the_hidden_folders_and_not_its_repos(box):
    pol = policy.loads('[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["~/work/**"]\n')
    cls = pol.classes["alpha"]
    home = wallsettings.class_home(cls, "linux")
    sealed = wallsettings.sealed_paths(cls, home, "linux", pol)
    assert sealed[0] == "~/.*/**" and "~/**" not in sealed  # `~/**` would deny the class's own repos too
    assert wallsettings.class_roots(cls, home) == [f"{home}/work"]


def test_sealed_paths_name_ws_folders_from_ws_paths_never_a_literal(box, tmp_path):
    from ws import paths

    pol = policy.loads('[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["/srv/alpha/**"]\n')
    cls = pol.classes["alpha"]
    home = "/home/ws-alpha"
    sealed = wallsettings.sealed_paths(cls, home, "linux", pol)
    folders = {str(d) for d in paths.protected_roots({"HOME": home})}
    assert folders and {f"~{d[len(home) :]}/**" for d in folders} <= set(sealed)  # every ws folder, home-relative
    assert {"~/.config/ws/**", "~/.local/state/ws/**", "~/.local/share/ws/**", "~/.cache/ws/**", "~/.ws/**"} <= set(
        sealed
    )
    elsewhere = tmp_path / "ws elsewhere"  # a folder the printing environment names outright is sealed too
    named = wallsettings.sealed_paths(cls, home, "linux", pol, {"WS_HOME": str(elsewhere), "XDG_STATE_HOME": "/x/y"})
    assert f"/{elsewhere}/**" in named and not any("/x/y" in p for p in named)  # only the outright names, not XDG_*
    assert f"/{elsewhere}/**" not in sealed
    src = Path(wallsettings.__file__).read_text(encoding="utf-8")
    assert '"~/.ws/**"' not in src  # no hand-spelled ws folder


def test_other_roots_skips_a_class_with_no_os_user(box):
    pol = policy.loads(
        '[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["/srv/alpha/**"]\n'
        '[classes.plain]\naccess = "full"\nrepos = ["~/code/**"]\n'
        '[classes.beta]\naccess = "walled"\nos_user = "ws-beta"\nrepos = ["/srv/beta/**"]\n'
    )
    cls = pol.classes["alpha"]
    got = wallsettings.launch_settings(cls, "/home/ws-alpha", "linux", pol)
    assert got["sandbox"]["filesystem"]["denyRead"] == ["/srv/beta"]  # `plain` has no account, so no `/home/...` guess
    assert not any("/home/" in p and "code" in p for p in got["permissions"]["deny"])


# --- the printed set-up -------------------------------------------------------------------------------------------------


def one_class(box):
    pol = policy.loads('[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["/srv/work/**"]\n')
    return pol, pol.classes["work"]


@pytest.mark.parametrize("kind", ["linux", "macos"])
def test_the_removal_steps_paste_and_run_in_bash(box, kind):
    import shutil
    import subprocess

    pol, cls = one_class(box)
    lines = wallsettings.removal_text(cls, kind)
    text = "\n".join(lines) + "\n"
    for line in lines:  # a note is a `#` line of its own; every other line is a command that starts with a word
        body = line.strip()
        assert body.startswith("#") or body.split()[0] in {"sudo", "chmod"}, line
        assert "  (" not in line and not body.startswith("(")
    managed = wallsettings.class_file_path("work", kind)
    launch = wallsettings.launch_file_path("work", kind)
    assert f"{managed}.tmp" in text and f"{launch}.tmp" in text  # a half-placed copy goes too
    assert f"sudo rmdir {launch.parent}" in text  # the per-class folder
    assert "the mode it had before step 2" in text
    assert ("sudo userdel --remove ws-work" in text) == (kind != "macos")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this machine")
    done = subprocess.run([bash, "-n"], check=False, input=text, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    whole = wallsettings.install_text(cls, {"HOME": str(box)}, kind, pol).split("8. To take it all out again:\n", 1)[1]
    assert whole.rstrip("\n").splitlines() == lines  # the install text prints the same lines


def test_the_install_text_places_both_files_and_prints_each_one_whole(box):
    pol, cls = one_class(box)
    text = wallsettings.install_text(cls, {"HOME": str(box)}, "linux", pol)
    home = wallsettings.class_home(cls, "linux")
    managed = text.split("<<'WS_CLASS_FILE'\n", 1)[1].split("\nWS_CLASS_FILE", 1)[0]
    launch = text.split("<<'WS_LAUNCH_FILE'\n", 1)[1].split("\nWS_LAUNCH_FILE", 1)[0]
    assert managed + "\n" == wallsettings.class_file_text(cls, home, "linux")
    assert launch + "\n" == wallsettings.launch_file_text(cls, home, "linux", pol, {"HOME": str(box)})
    assert (
        str(wallsettings.class_file_path("work", "linux")) in text
        and str(wallsettings.launch_file_path("work", "linux")) in text
    )
    assert "sudo chown root:root" in text and "applies to every account on this machine" in text
    assert "ws walls check" in text and "WS014" in text


# --- the walls' dotfiles, the merge order, and what the project check walks ----------------------------------------------------


def test_a_class_with_repos_in_its_home_seals_its_top_level_dotfiles_as_well_as_its_dot_folders(box):
    pol = policy.loads('[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["~/work/**"]\n')
    cls = pol.classes["alpha"]
    home = wallsettings.class_home(cls, "linux")
    sealed = wallsettings.sealed_paths(cls, home, "linux", pol)
    assert "~/.*/**" in sealed and "~/.*" in sealed  # `~/.*/**` alone misses `~/.claude.json` and `~/.bashrc`
    deny = wallsettings.launch_settings(cls, home, "linux", pol)["permissions"]["deny"]
    assert {"Read(~/.*)", "Edit(~/.*)", "Read(~/.*/**)", "Edit(~/.*/**)"} <= set(deny)
    elsewhere = policy.loads('[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["/srv/alpha/**"]\n')
    outside = wallsettings.sealed_paths(elsewhere.classes["alpha"], home, "linux", elsewhere)
    assert "~/**" in outside and "~/.*" not in outside  # the whole home is sealed, so there is nothing to add


def test_an_allow_read_entry_under_another_class_is_dropped_and_named(lbox, monkeypatch, capsys):
    box, _pol, started = lbox
    (box / "work" / "beta" / "shared").mkdir()
    (box / "work" / "alpha" / "ok").mkdir()
    toml = f'[gate]\nallow_read = ["{box}/work/beta/shared", "{box}/work/alpha/ok"]\n' + TWO.format(box=box)
    (box / ".ws" / "policy.toml").write_text(toml, encoding="utf-8")
    place_class_file("alpha", policy.loads(toml), monkeypatch)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0
    conf = session_conf(started)
    reads = conf["sandbox"]["filesystem"]["allowRead"]
    assert str(box.resolve() / "work" / "alpha" / "ok") in reads  # an entry of its own class stays
    assert not any(r.startswith(str(box.resolve() / "work" / "beta")) for r in reads)
    assert f"{box}/work/beta" in denies(conf)
    err = capsys.readouterr().err
    assert "inside another class's folder" in err and f"{box}/work/beta/shared" in err and "work/alpha/ok" not in err


def test_a_big_project_with_links_outside_dot_claude_still_starts(lbox, monkeypatch):
    box, pol, started = lbox
    place_class_file("alpha", pol, monkeypatch)
    app = box / "work" / "alpha" / "app"
    src = app / "src"
    src.mkdir()
    for n in range(5000):
        (src / f"f{n}.py").write_text("x\n")
    (app / "vendor").symlink_to(box / "work" / "free")  # a linked source folder
    (app / "README.link").symlink_to(src / "f0.py")
    assert wallsettings.project_problem([str(app)], box / "nohome") is None
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 0 and started


def test_a_dot_claude_tree_over_the_limit_or_with_a_link_is_still_refused(lbox, monkeypatch):
    box, pol, started = lbox
    app = box / "work" / "alpha" / "app"
    nohome = box / "nohome"
    big = app / ".claude" / "skills" / "bulk"
    big.mkdir(parents=True)
    for n in range(wallsettings._MAX_FILES + 1):
        (big / f"n{n}.txt").write_text("x\n")
    refused = wallsettings.project_problem([str(app)], nohome)
    assert refused.kind == "project_settings_unreadable" and "too many files" in refused.message
    shutil.rmtree(app / ".claude")
    (app / ".claude" / "skills" / "s").mkdir(parents=True)
    (app / ".claude" / "skills" / "s" / "link").symlink_to(box / "work" / "free")
    assert "is a link" in wallsettings.project_problem([str(app)], nohome).message
    place_class_file("alpha", pol, monkeypatch)
    assert launch_as(box, monkeypatch, "ws-alpha", "alpha") == 3 and started == []
