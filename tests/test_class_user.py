"""A class with an `os_user` (C52): the managed-settings file ws prints for it (never writes), the WS014 doctor rows, and the
launch that refuses a session started as the wrong account.

Most of this runs anywhere: the account the policy names is a name the machine does not know, or the account running the
tests. The OS-level half of the cross-home test uses a second real account, the ready-made one named in
WS_TEST_CLASS_USER (ws never creates one); without it, or without passwordless `sudo -u` to it, or on a system with no
user database, only the half that needs no account runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from ws import cli, doctor, osinfo, policy, sandbox, wallsettings
from ws.output import WsError

CLASS_USER = os.environ.get("WS_TEST_CLASS_USER", "")
POSIX = sys.platform in ("linux", "darwin")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "work" / "app").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "work" / "app")
    drop = tmp_path / "managed-settings.d"
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(drop),))
    monkeypatch.setattr(wallsettings, "MANAGED_FILES", (str(tmp_path / "managed-settings.json"),))
    return tmp_path


def me() -> str:
    return sandbox.real_user() or ""


def write_policy(box, user: str, inbound: str = "") -> policy.Policy:
    extra = f'inbound = "{inbound}"\n' if inbound else ""
    text = f'[classes.work]\naccess = "walled"\nos_user = "{user}"\nrepos = ["{box}/work/**"]\n{extra}'
    pol = box / "home" / ".ws" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(text, encoding="utf-8")
    return policy.load(pol)


def place(box, pol: policy.Policy, monkeypatch, edit=None):
    """The class file ws renders, placed in the managed folder; the test is not root, so root ownership is stood in for."""
    cls = pol.classes["work"]
    conf = wallsettings.class_settings(cls, str(box / "home"), "linux")
    if edit:
        edit(conf)
    file = wallsettings.class_file_path("work", "linux")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(conf), encoding="utf-8")
    file.chmod(0o644)
    file.parent.chmod(0o755)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    return file


def rows(box, pol) -> dict[str, wallsettings.Row]:
    got = wallsettings.class_rows(pol, {"HOME": str(box / "home")}, "linux")
    assert [r.part for r in got] == list(wallsettings.PARTS)  # one row per part, in order, for the one class
    return {r.part: r for r in got}


# --- doctor: each part on its own ----------------------------------------------------------------------------------------


def test_doctor_reports_each_part_of_a_class_separately(box, monkeypatch):
    pol = write_policy(box, me() or CLASS_USER or "someone")
    r = rows(box, pol)  # no file yet: the user may exist, nothing else does
    assert r["OS user exists"].status == (wallsettings.OK if wallsettings.passwd(me()) else wallsettings.WARN)
    assert (
        r["file present and root-owned"].status == "warn"
        and "no managed-settings file" in r["file present and root-owned"].detail
    )
    assert all(r[p].status == "warn" for p in wallsettings.CONTENT_PARTS)
    place(box, pol, monkeypatch)
    r = rows(box, pol)
    assert {p: x.status for p, x in r.items() if p != "OS user exists"} == dict.fromkeys(wallsettings.PARTS[1:], "ok")

    def broken(part: str, edit) -> dict[str, str]:
        place(box, pol, monkeypatch, edit)
        got = rows(box, pol)
        return {p: x.status for p, x in got.items() if x.status != "ok" and p != "OS user exists"}

    def unsandbox(c):
        c["sandbox"]["allowUnsandboxedCommands"] = True

    def optional(c):
        c["sandbox"]["failIfUnavailable"] = False

    def bypass(c):
        c["permissions"][wallsettings.BYPASS_KEY] = "off"

    def write(c):
        c["sandbox"]["filesystem"]["allowWrite"].append("/srv/elsewhere")

    def home_open(c):
        c["sandbox"]["filesystem"]["allowRead"].append("~/.ssh")

    def hidden(c):
        c["sandbox"]["filesystem"]["denyRead"] = []

    def inbound(c):
        c["env"]["WS_INBOUND"] = "anyone"

    assert broken("unsandboxed", unsandbox) == {"unsandboxed commands off": "fail"}
    assert broken("required", optional) == {"sandbox required": "fail"}
    assert broken("bypass", bypass) == {"bypass off": "fail"}
    assert broken("write", write) == {"write limits": "fail"}
    assert broken("read", home_open) == {"home deny-read": "fail"}
    assert broken("hidden", hidden) == {"home deny-read": "fail"}
    assert broken("inbound", inbound) == {"inbound lead-only": "fail"}


def test_a_class_file_that_others_can_write_or_a_user_owns_is_reported_not_trusted(box, monkeypatch):
    pol = write_policy(box, me() or "someone")
    file = place(box, pol, monkeypatch)
    file.chmod(0o666)
    r = rows(box, pol)["file present and root-owned"]
    assert r.status == "fail" and "writable by group or others" in r.detail and "chmod go-w" in r.fix
    file.chmod(0o644)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: False)  # the file belongs to whoever ran the test
    r = rows(box, pol)["file present and root-owned"]
    assert r.status == "fail" and "not owned by root" in r.detail
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    file.unlink()
    file.symlink_to(box / "elsewhere.json")
    assert rows(box, pol)["file present and root-owned"].status == "fail"


def test_doctor_rows_say_which_part_is_missing_and_name_the_class_not_the_account(box):
    ghost = "ws-no-such-user-x"
    write_policy(box, ghost)
    checks = doctor.run_checks(osinfo.detect(), box / "home" / ".ws" / "profile.toml", dict(os.environ), network=False)
    ws014 = [c for c in checks if c.id == "WS014"]
    assert [c.detail.split(": ")[1] for c in ws014] == list(wallsettings.PARTS)
    assert all(c.detail.startswith("work: ") for c in ws014) and not any(ghost in c.detail + c.fix for c in ws014)
    first = ws014[0]
    assert first.status == "warn" and "user database" in first.detail and "ws install --class work" in first.fix
    assert not [c for c in ws014 if c.status in doctor.BAD]  # warnings only: nothing is in place to be wrong


def test_a_wildcard_repo_folder_is_a_warn_row_not_a_wide_file(box, monkeypatch):
    """`work/ops-*` cannot be held as a folder: the rows say so, and no render reaches past the repos."""
    pol_file = box / "home" / ".ws" / "policy.toml"
    place(box, write_policy(box, me() or "someone"), monkeypatch)  # a file in place, then the repo becomes a wildcard
    pol_file.write_text(pol_file.read_text().replace(f'"{box}/work/**"', f'"{box}/work/ops-*"'), encoding="utf-8")
    pol = policy.load(pol_file)
    with pytest.raises(WsError) as e:
        wallsettings.class_roots(pol.classes["work"], str(box / "home"))
    assert e.value.kind == "usage"
    got = rows(box, pol)
    assert not [r for r in got.values() if r.status == "fail"]
    assert "wildcard in a folder name" in got["sandbox required"].detail


def test_a_policy_without_an_os_user_class_adds_no_ws014_line(box):
    pol = box / "home" / ".ws" / "policy.toml"
    pol.parent.mkdir(parents=True)
    pol.write_text('[classes.work]\naccess = "walled"\nrepos = ["/srv/work/**"]\n', encoding="utf-8")
    checks = doctor.run_checks(osinfo.detect(), box / "home" / ".ws" / "profile.toml", dict(os.environ), network=False)
    assert not [c for c in checks if c.id == "WS014"]


def test_inbound_allowlist_is_a_warn_in_the_inbound_row(box, monkeypatch):
    pol = write_policy(box, me() or "someone")
    text = (box / "home" / ".ws" / "policy.toml").read_text() + 'inbound = "allowlist"\ninbound_allow = ["lead"]\n'
    (box / "home" / ".ws" / "policy.toml").write_text(text)
    pol = policy.load(box / "home" / ".ws" / "policy.toml")
    place(box, pol, monkeypatch)
    row = rows(box, pol)["inbound lead-only"]
    assert row.status == "warn" and "allowlist" in row.detail


# --- launch: the wrong account -------------------------------------------------------------------------------------------


def launch(capsys, box, *extra):
    code = cli.main(
        [
            "gate",
            "launch",
            "--runner",
            "claude-code",
            "--policy",
            str(box / "home" / ".ws" / "policy.toml"),
            *extra,
            "--",
            "claude",
        ]
    )
    return code, capsys.readouterr()


def test_launch_refuses_a_class_session_as_the_wrong_user(box, monkeypatch, capsys):
    other = CLASS_USER if CLASS_USER and CLASS_USER != me() else "ws-other-account-x"
    write_policy(box, other)

    def started(*a, **k):
        raise AssertionError("nothing may start for a session of the wrong account")

    monkeypatch.setattr(sandbox, "run_plain", started)
    monkeypatch.setattr(sandbox, "run_with_broker", started)
    monkeypatch.setattr(sandbox, "scan_project", started)
    monkeypatch.setattr(os, "execvpe", started)
    for argv in ((), ("--no-sandbox",)):
        code, out = launch(capsys, box, *argv)
        assert code == 3 and "class 'work'" in out.err and "OS user" in out.err
        assert other not in out.err  # the account is named by its class only
    assert not (box / "home" / ".ws" / "gate").exists()  # nothing was written
    # a forged environment changes nothing: the user is the one the system says, not $USER, $LOGNAME or $HOME
    for name in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.setenv(name, other)
    monkeypatch.setenv("HOME", str(box / "forged"))
    assert launch(capsys, box)[0] == 3
    with pytest.raises(WsError) as e:
        sandbox.require_class_user(policy.load(box / "home" / ".ws" / "policy.toml"))
    assert e.value.exit_code == 3 and e.value.kind == "refused" and "work" in e.value.hint and other not in e.value.hint


def test_launch_lets_the_right_user_and_unclassified_folders_through(box, monkeypatch, tmp_path):
    pol = write_policy(box, me() or "someone")
    real = me() or "someone"
    monkeypatch.setattr(sandbox, "real_user", lambda: real)
    sandbox.require_class_user(pol)  # the class user, in a folder of the class
    monkeypatch.setattr(sandbox, "real_user", lambda: None)  # a user the database does not know is refused
    with pytest.raises(WsError):
        sandbox.require_class_user(pol)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    monkeypatch.setattr(sandbox, "real_user", lambda: "an-account-no-class-names")
    sandbox.require_class_user(pol)  # a folder in no class is not held against an account that is no class's
    monkeypatch.chdir(tmp_path / "work" / "app")
    sandbox.require_class_user(policy.loads(""))  # no [classes]: today's behaviour


def test_a_class_account_is_refused_in_a_folder_of_no_class(box, monkeypatch, tmp_path):
    pol = write_policy(box, "someone")
    monkeypatch.setattr(sandbox, "real_user", lambda: "someone")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    with pytest.raises(WsError) as e:
        sandbox.require_class_user(pol)
    assert e.value.exit_code == 3 and e.value.kind == "refused"
    assert "class 'work'" in e.value.message + e.value.hint and "no class" in e.value.message
    assert "someone" not in e.value.message + e.value.hint  # the class is named, not the account
    sandbox.require_class_user(pol, str(tmp_path / "work" / "app"))  # its own class's folder is fine, from anywhere
    with pytest.raises(WsError):
        sandbox.require_class_user(pol, str(tmp_path / "elsewhere"))  # a job's folder is judged, not this process's


def test_a_class_account_is_refused_in_a_different_class_folder(box, monkeypatch, tmp_path):
    text = (
        f'[classes.work]\naccess = "walled"\nos_user = "someone"\nrepos = ["{box}/work/**"]\n'
        f'[classes.other]\naccess = "full"\nrepos = ["{box}/other/**"]\n'
    )
    pol_file = box / "home" / ".ws" / "policy.toml"
    pol_file.parent.mkdir(parents=True, exist_ok=True)
    pol_file.write_text(text, encoding="utf-8")
    pol = policy.load(pol_file)
    (box / "other" / "repo").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "real_user", lambda: "someone")
    with pytest.raises(WsError) as e:
        sandbox.require_class_user(pol, str(box / "other" / "repo"))
    assert e.value.exit_code == 3 and "class 'other'" in e.value.message and "class 'work'" in e.value.hint
    assert "someone" not in e.value.message + e.value.hint
    monkeypatch.setattr(sandbox, "real_user", lambda: "a-different-account")
    sandbox.require_class_user(pol, str(box / "other" / "repo"))  # the person's own account is not held to this


def test_launch_refuses_a_class_account_outside_its_class(box, monkeypatch, capsys, tmp_path):
    write_policy(box, "someone")
    monkeypatch.setattr(sandbox, "real_user", lambda: "someone")

    def started(*a, **k):
        raise AssertionError("nothing may start outside the class")

    monkeypatch.setattr(sandbox, "run_plain", started)
    monkeypatch.setattr(sandbox, "run_with_broker", started)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    for argv in ((), ("--no-sandbox",)):
        code, out = launch(capsys, box, *argv)
        assert code == 3 and "class 'work'" in out.err and "no class" in out.err


def test_the_class_comes_from_the_real_path_of_the_folder_not_a_link_name(box, monkeypatch):
    write_policy(box, "ws-other-account-x")
    pol = policy.load(box / "home" / ".ws" / "policy.toml")
    (box / "plain").mkdir()
    (box / "plain" / "door").symlink_to(box / "work" / "app")
    monkeypatch.chdir(box / "plain" / "door")  # a link in an unclassified folder, into the class's repo
    with pytest.raises(WsError) as e:
        sandbox.require_class_user(pol)
    assert e.value.exit_code == 3


# --- the second account, for real ----------------------------------------------------------------------------------------


def can_become(user: str) -> str | None:
    """The path of `sudo` when this account may run commands as `user` without a password, else None."""
    sudo = shutil.which("sudo")
    if not (POSIX and user and sudo and wallsettings.passwd(user) is not None):
        return None
    ok = subprocess.run([sudo, "-n", "-u", user, "true"], check=False, capture_output=True).returncode == 0
    return sudo if ok else None


def test_a_cross_home_read_is_refused(box):
    """The class file hides the home folder from the class's account, and the operating system refuses that account the
    other account's home: the first half runs everywhere; the second needs the ready-made account in WS_TEST_CLASS_USER
    and passwordless `sudo -u` to it, and says nothing where there is none."""
    cls = write_policy(box, CLASS_USER or "ws-other-account-x").classes["work"]
    fs = wallsettings.class_settings(cls, str(box / "home"), "linux")["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["~/"] and str(box / "home") not in "".join(fs["allowRead"])
    assert all(r.startswith(str(box / "work")) for r in fs["allowRead"])  # the repos only, none of them a home folder
    sudo = can_become(CLASS_USER)
    if sudo is None:
        return
    secret = box / "home" / "private" / "notes.txt"
    secret.parent.mkdir()
    secret.write_text("mine\n")
    for d in (secret.parent, box / "home"):
        d.chmod(0o700)
    got = subprocess.run(
        [sudo, "-n", "-u", CLASS_USER, "cat", str(secret)], check=False, capture_output=True, text=True
    )
    assert got.returncode != 0 and "mine" not in got.stdout


# --- off by default ------------------------------------------------------------------------------------------------------


def test_managed_file_is_not_rendered_by_default(box, monkeypatch, capsys):
    write_policy(box, me() or "someone")

    def never(*a, **k):
        raise AssertionError("the managed file is rendered only by `ws install --class`")

    monkeypatch.setattr(wallsettings, "class_file_text", never)
    monkeypatch.setattr(wallsettings, "install_text", never)
    argv = ["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes")]
    assert cli.main(argv) == 0
    assert cli.main(["gate", "generate", "--runner", "claude-code", "--out", str(box / "out")]) == 0
    cli.main(["doctor", "--offline"])
    capsys.readouterr()
    assert not os.path.lexists(wallsettings.MANAGED_DIRS[0]) and not os.path.lexists(wallsettings.MANAGED_FILES[0])
    assert not [p for p in box.rglob("*") if "ws-class-" in p.name]
    assert "ws-class-" not in (box / "out" / "settings.json").read_text()


# --- a `~` repo before the account exists; a file the account can't read; a fix for each problem --------------------------


def tilde_class(box, user: str = "ws-no-such-user-x") -> policy.Class:
    text = f'[classes.work]\naccess = "walled"\nos_user = "{user}"\nrepos = ["~/work/**", "~/other"]\n'
    pol = box / "home" / ".ws" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(text, encoding="utf-8")
    return policy.load(pol).classes["work"]


@pytest.mark.parametrize("kind, home", [("linux", "/home/ws-no-such-user-x"), ("macos", "/Users/ws-no-such-user-x")])
def test_a_tilde_repo_before_the_account_exists_means_the_home_the_printed_step_makes(box, kind, home):
    cls = tilde_class(box)
    assert wallsettings.passwd(cls.os_user) is None
    assert wallsettings.class_home(cls, kind) == home
    text = wallsettings.install_text(cls, {"HOME": str(box / "home")}, kind)
    assert f"chown -R ws-no-such-user-x {home}/work" in text and f"chown -R ws-no-such-user-x {home}/other" in text
    assert f"chmod 700 {home}" in text  # step 1 closes the same home the repos are under
    body = json.loads(text.split("<<'WS_CLASS_FILE'\n", 1)[1].split("\nWS_CLASS_FILE", 1)[0])
    fs = body["sandbox"]["filesystem"]
    assert fs["allowWrite"][:2] == [f"{home}/work", f"{home}/other"] and fs["allowRead"] == [
        f"{home}/work",
        f"{home}/other",
    ]
    assert str(box / "home") not in text.replace(f"{box}/home/.ws", "")  # nothing names the person's own folders


def test_the_file_ws_prints_is_the_file_doctor_expects_once_the_account_exists(box, monkeypatch):
    """Printed with no account, the file is under `/home/NAME`; when the system then knows the account at that home,
    every WS014 content row agrees with what was placed."""
    cls = tilde_class(box, user="someone")
    printed = json.loads(wallsettings.class_file_text(cls, wallsettings.class_home(cls, "linux"), "linux"))
    file = wallsettings.class_file_path("work", "linux")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(printed), encoding="utf-8")
    file.chmod(0o644)
    file.parent.chmod(0o755)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)

    class Entry:
        pw_dir = "/home/someone"

    monkeypatch.setattr(wallsettings, "passwd", lambda name: Entry())
    got = wallsettings.class_rows(
        policy.load(box / "home" / ".ws" / "policy.toml"), {"HOME": str(box / "home")}, "linux"
    )
    bad = {r.part: r.status for r in got if r.status not in ("ok", "warn")}
    assert bad == {}, bad


def test_a_note_says_when_a_repo_is_under_your_own_home(box):
    pol = write_policy(box, "ws-no-such-user-x")  # the repos are under the box, which holds the home
    cls = pol.classes["work"]
    text = wallsettings.install_text(cls, {"HOME": str(box)}, "linux")
    assert "is under your own home" in text
    assert "is under your own home" not in wallsettings.install_text(cls, {"HOME": str(box / "home")}, "linux")


def test_a_file_the_account_cannot_read_is_a_failure_with_the_command_that_fixes_it(box, monkeypatch):
    pol = write_policy(box, me() or "someone")
    file = place(box, pol, monkeypatch)
    for mode in (0o600, 0o640, 0o000):
        file.chmod(mode)
        r = rows(box, pol)["file present and root-owned"]
        assert r.status == "fail" and "can't be read" in r.detail, oct(mode)
        assert r.fix == f"sudo chmod 644 {file}"
    file.chmod(0o644)
    assert rows(box, pol)["file present and root-owned"].status == "ok"


def test_each_problem_with_the_class_file_gets_its_own_fix(box, monkeypatch):
    pol = write_policy(box, me() or "someone")
    file = place(box, pol, monkeypatch)

    def row():
        return rows(box, pol)["file present and root-owned"]

    file.chmod(0o666)
    assert row().fix == f"sudo chmod go-w {file}"
    file.chmod(0o644)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: False)
    assert row().fix == f"sudo chown root {file}"
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    file.parent.chmod(0o777)  # a folder others can write: the fix is about the folder, not the file
    r = row()
    assert r.status == "fail" and "folder" in r.detail
    assert r.fix == f"sudo chown root {file.parent} && sudo chmod go-w {file.parent}"
    file.parent.chmod(0o755)
    file.unlink()
    file.symlink_to(box / "elsewhere.json")
    r = row()
    assert r.status == "fail" and r.fix.startswith(f"sudo rm {file}, then place the file again")


def test_the_inbound_row_says_the_file_records_it(box, monkeypatch):
    pol = write_policy(box, me() or "someone")
    place(box, pol, monkeypatch)
    r = rows(box, pol)["inbound lead-only"]
    assert r.status == "ok" and "recorded as lead-only in the file" in r.detail
