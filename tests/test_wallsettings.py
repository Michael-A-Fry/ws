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


def test_the_launch_settings_and_the_class_file_carry_the_read_block(box):
    assert launch_settings(box)["permissions"][wallsettings.READ_BLOCK_KEY] is True
    cls = wallsettings.policy.loads(
        '[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["/srv/work/**"]\n'
    ).classes["work"]
    assert wallsettings.class_settings(cls, str(box), "linux")["permissions"][wallsettings.READ_BLOCK_KEY] is True


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


def test_ws014_is_ok_for_a_class_file_that_holds_the_read_block(box, monkeypatch, tmp_path, capsys):
    row = class_rows_with(box, monkeypatch, tmp_path, capsys)["read block"]
    assert row.status == "ok" and wallsettings.READ_BLOCK_KEY in row.detail and not row.fix


def test_ws014_warns_for_a_class_file_placed_before_the_read_block_and_says_how_to_place_it_again(
    box, monkeypatch, tmp_path, capsys
):
    rows = class_rows_with(
        box, monkeypatch, tmp_path, capsys, lambda c: c["permissions"].pop(wallsettings.READ_BLOCK_KEY)
    )
    row = rows["read block"]
    assert row.status == "warn" and wallsettings.READ_BLOCK_KEY in row.detail
    assert "ws install --class work" in row.fix  # never a fail, never a crash
    assert all(r.status != "fail" for r in rows.values())


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


def test_ws014_does_not_say_ok_when_the_read_block_is_false(box, monkeypatch, tmp_path, capsys):
    def off(c):
        c["permissions"][wallsettings.READ_BLOCK_KEY] = False

    assert class_rows_with(box, monkeypatch, tmp_path, capsys, off)["read block"].status == "warn"
