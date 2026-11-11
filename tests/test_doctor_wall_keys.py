"""WS016: Claude Code is new enough to read the settings keys the wall writes (one table, wallsettings.MIN_CLAUDE), and the
permission mode in force is not bypass. The version is probed and parsed as WS011 does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import doctor, osinfo, policy, run, wallsettings

READ_BLOCK = f"permissions.{wallsettings.READ_BLOCK_KEY}"
ALLOW_READ = "sandbox.filesystem.allowRead"


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
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


@pytest.fixture
def claude(monkeypatch, box):
    """`claude` is on PATH and prints the version the test sets."""
    exe = box / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = {"version": "2.1.321"}
    monkeypatch.setattr(run, "exe_version", lambda path, env=None, timeout=10.0: seen["version"])
    return seen, {"PATH": str(exe.parent), "HOME": str(box / "home"), "WS_HOME": str(box / "home" / ".ws")}


def ws016(env) -> list[doctor.Check]:
    return doctor._wall_keys_check(env)


@pytest.mark.parametrize("version", ["2.1.257", "2.1.321", "2.2.0", "3.0.0", "2.1.321-beta"])
def test_a_claude_code_that_reads_the_wall_keys_is_ok(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = ws016(env)
    assert (check.id, check.role, check.status) == ("WS016", "wall-keys", "ok")
    assert version in check.detail and not check.fix


@pytest.mark.parametrize("version", ["2.1.256", "2.1.200", "2.0.999", "1.9.9"])
def test_an_older_claude_code_is_a_warning_naming_the_key_and_the_fix(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = ws016(env)
    assert check.status == "warn" and READ_BLOCK in check.detail and "2.1.257" in check.detail
    assert check.fix == "update Claude Code"


@pytest.mark.parametrize("version", ["", "nightly", None])
def test_a_version_that_cannot_be_read_is_a_warning_and_not_a_crash(claude, version):
    seen, env = claude
    seen["version"] = version
    (check,) = ws016(env)
    assert check.status == "warn" and "2.1.257" in check.detail and "update Claude Code" in check.fix


def test_no_claude_code_is_ok_not_applicable(box):
    (check,) = ws016({"PATH": str(box / "nowhere")})
    assert (check.id, check.status) == ("WS016", "ok") and check.detail.startswith("not applicable")


def test_the_table_is_the_one_the_check_and_the_adapter_use(claude):
    assert wallsettings.MIN_CLAUDE[READ_BLOCK] == (2, 1, 257)
    assert all(doctor._version_tuple(".".join(map(str, v))) == v for v in wallsettings.MIN_CLAUDE.values())
    seen, env = claude
    seen["version"] = "2.1.256"  # one below the table's version: the check reads the table, not a number of its own
    assert ws016(env)[0].status == "warn"
    seen["version"] = ".".join(map(str, wallsettings.MIN_CLAUDE[READ_BLOCK]))
    assert ws016(env)[0].status == "ok"


def test_a_policy_that_leaves_the_block_out_has_no_key_for_an_old_claude_code_to_miss(claude, box):
    # with the sandbox off too: with it on, the launch settings write allowRead (the next test)
    seen, env = claude
    seen["version"] = "2.0.0"
    (box / "home" / ".ws" / "policy.toml").write_text("[gate]\nread_outside_project = true\nsandbox = false\n")
    (check,) = ws016(env)
    assert check.status == "ok" and "no settings key" in check.detail


def test_the_launch_settings_deny_home_key_is_in_the_table_and_checked_with_the_sandbox_on(claude, box):
    assert wallsettings.MIN_CLAUDE[ALLOW_READ] == (2, 1, 78)  # 2.1.77 added allowRead, 2.1.78 fixed absolute paths
    seen, env = claude
    (box / "home" / ".ws" / "policy.toml").write_text("[gate]\nread_outside_project = true\n")  # no read block
    seen["version"] = "2.1.77"
    (check,) = ws016(env)
    assert check.status == "warn" and ALLOW_READ in check.detail and "2.1.78" in check.detail
    assert READ_BLOCK not in check.detail and check.fix == "update Claude Code"
    seen["version"] = "2.1.78"
    assert ws016(env)[0].status == "ok"
    assert ALLOW_READ not in wallsettings.keys_written(policy.loads("[gate]\nsandbox = false\n"))
    assert ALLOW_READ in wallsettings.keys_written(policy.default_policy())


def test_a_data_class_adds_the_strict_allowlist_key_to_what_is_checked(claude, box):
    seen, env = claude
    seen["version"] = "2.1.230"  # reads strictAllowlist, not the read block
    (box / "home" / ".ws" / "policy.toml").write_text('[classes.work]\naccess = "walled"\nrepos = ["/srv/w/**"]\n')
    (check,) = ws016(env)
    assert check.status == "warn" and READ_BLOCK in check.detail and "strictAllowlist" not in check.detail
    seen["version"] = "2.1.200"
    (check,) = ws016(env)
    assert "sandbox.network.strictAllowlist" in check.detail


def test_a_policy_that_does_not_load_is_judged_by_the_default_policy_not_a_crash(claude, box):
    seen, env = claude
    seen["version"] = "2.1.100"
    (box / "home" / ".ws" / "policy.toml").write_text("[gate]\nread_outside_project = 3\n")
    (check,) = ws016(env)
    assert check.status == "warn" and READ_BLOCK in check.detail


# --- bypass mode ----------------------------------------------------------------------------------------------------


def put(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_bypass_mode_in_force_is_its_own_warning_line(claude, box):
    _, env = claude
    f = put(box / "home" / ".claude" / "settings.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    key, bypass = ws016(env)
    assert key.status == "ok"
    assert (bypass.id, bypass.status) == ("WS016", "warn")
    assert "bypassPermissions" in bypass.detail and str(f) in bypass.detail and str(f) in bypass.fix


def test_a_mode_that_asks_is_not_flagged(claude, box):
    _, env = claude
    put(box / "home" / ".claude" / "settings.json", {"permissions": {"defaultMode": "acceptEdits"}})
    assert [c.status for c in ws016(env)] == ["ok"]


def test_a_project_file_cannot_make_bypass_in_force(claude, box):
    _, env = claude
    put(box / "work" / ".claude" / "settings.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    assert [c.status for c in ws016(env)] == ["ok"]  # Claude Code ignores bypass from a project file


# --- in the doctor run ----------------------------------------------------------------------------------------------


def test_the_check_runs_after_ws015_and_a_crash_is_a_fail_line_not_a_crash(claude, box, monkeypatch):
    _, env = claude

    def boom(env):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(doctor, "_wall_keys_check", boom)
    checks = doctor.run_checks(osinfo.detect(), box / "profile.toml", {**env, "PATH": env["PATH"]}, network=False)
    ids = [c.id for c in checks]
    assert ids.index("WS016") > ids.index("WS013")  # WS014 and WS015 only have lines when the policy has classes
    line = next(c for c in checks if c.id == "WS016")
    assert line.status == "fail" and line.detail == "check crashed (RuntimeError)" and "secret" not in line.detail


def test_the_line_is_in_the_doctor_and_in_the_reference(claude, box):
    _, env = claude
    checks = doctor.run_checks(osinfo.detect(), box / "profile.toml", env, network=False)
    ids = [c.id for c in checks]
    assert "WS016" in ids
    page = (Path(__file__).resolve().parents[1] / "docs" / "reference" / "doctor-checks.md").read_text(encoding="utf-8")
    assert "| WS016 | wall-keys |" in page
