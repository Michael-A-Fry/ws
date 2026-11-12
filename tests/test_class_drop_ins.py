"""WS019 and the `ws install --class` warning: the ws class drop-ins in the managed-settings folder. More than one in place,
or one for a class the policy doesn't name, is a warning (one class per machine is the tested shape); the drop-in ws
renders for a class is the same whatever else is in the folder. Everything runs under a tmp HOME, WS_HOME and managed folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import cli, doctor, osinfo, policy, wallsettings

POLICY = (
    '[classes.alpha]\naccess = "walled"\nos_user = "ws-alpha"\nrepos = ["/srv/alpha/**"]\n'
    '[classes.beta]\naccess = "walled"\nos_user = "ws-beta"\nrepos = ["/srv/beta/**"]\n'
)
KIND = osinfo.detect().kind  # the managed folder is one tmp folder, so any kind reads it


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    managed = tmp_path / "managed" / "managed-settings.d"
    managed.mkdir(parents=True)
    monkeypatch.setattr(wallsettings, "MANAGED_FILES", (str(tmp_path / "managed" / "managed-settings.json"),))
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(managed),))
    (home / ".ws" / "policy.toml").write_text(POLICY, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


def put(box, *names: str) -> None:
    for n in names:
        (box / "managed" / "managed-settings.d" / wallsettings.class_file_name(n)).write_text("{}", encoding="utf-8")


def env_of(box) -> dict[str, str]:
    return {"HOME": str(box / "home"), "WS_HOME": str(box / "home" / ".ws")}


def ws019(box) -> list[doctor.Check]:
    return doctor._drop_in_checks(env_of(box), KIND)


def test_only_ws_class_drop_ins_are_listed_in_name_order(box):
    put(box, "beta", "alpha")
    folder = box / "managed" / "managed-settings.d"
    for other in ("10-site.json", "50-ws-class-.json", "50-ws-class-x.json.bak"):
        (folder / other).write_text("{}", encoding="utf-8")
    assert [p.name for p in wallsettings.ws_drop_ins(KIND)] == [
        wallsettings.class_file_name("alpha"),
        wallsettings.class_file_name("beta"),
    ]


def test_a_missing_managed_folder_lists_nothing(box, monkeypatch):
    monkeypatch.setattr(wallsettings, "MANAGED_DIRS", (str(box / "nowhere"),))
    assert wallsettings.ws_drop_ins(KIND) == []


def test_no_drop_in_has_no_line(box):
    assert ws019(box) == []


def test_one_drop_in_for_a_class_the_policy_names_is_ok(box):
    put(box, "alpha")
    (check,) = ws019(box)
    assert (check.id, check.role, check.status) == ("WS019", "class-drop-ins", "ok") and not check.fix


def test_two_drop_ins_are_a_warning_naming_the_classes_and_the_tested_shape(box):
    put(box, "alpha", "beta")
    (check,) = ws019(box)
    assert check.status == "warn" and "alpha, beta" in check.detail
    assert "one class per machine is the tested shape" in check.detail and check.fix
    assert str(box) not in check.detail  # classes, never a path


def test_a_drop_in_for_a_class_the_policy_does_not_name_is_a_warning(box):
    put(box, "gamma")
    (check,) = ws019(box)
    assert check.status == "warn" and "gamma" in check.detail and "no class of this policy" in check.detail


def test_a_policy_that_does_not_load_still_counts_the_drop_ins(box):
    (box / "home" / ".ws" / "policy.toml").write_text("[gate\n", encoding="utf-8")
    put(box, "alpha")
    assert [c.status for c in ws019(box)] == ["ok"]
    put(box, "beta")
    assert [c.status for c in ws019(box)] == ["warn"]


def test_the_line_is_in_the_doctor_in_the_reference_and_a_crash_is_a_fail_line(box, monkeypatch):
    put(box, "alpha", "beta")
    checks = doctor.run_checks(osinfo.detect(), box / "profile.toml", env_of(box), network=False)
    assert "WS019" in [c.id for c in checks]
    page = (Path(__file__).resolve().parents[1] / "docs" / "reference" / "doctor-checks.md").read_text(encoding="utf-8")
    assert "| WS019 | class-drop-ins |" in page

    def boom(env, kind):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(doctor, "_drop_in_checks", boom)
    checks = doctor.run_checks(osinfo.detect(), box / "profile.toml", env_of(box), network=False)
    line = next(c for c in checks if c.id == "WS019")
    assert line.status == "fail" and line.detail == "check crashed (RuntimeError)" and "secret" not in line.detail


def test_install_class_warns_when_another_class_drop_in_is_in_place(box, capsys):
    put(box, "alpha")
    assert cli.main(["install", "--class", "beta"]) == 0
    got = capsys.readouterr()
    assert "alpha" in got.err and "one class per machine is the tested shape" in got.err
    assert "warning" not in got.out


def test_install_class_is_quiet_when_only_its_own_drop_in_is_in_place(box, capsys):
    put(box, "beta")
    assert cli.main(["install", "--class", "beta"]) == 0
    assert capsys.readouterr().err == ""


def test_install_class_json_carries_the_warning(box, capsys):
    put(box, "alpha")
    assert cli.main(["install", "--class", "beta", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["writes"] is False and len(data["warnings"]) == 1 and "alpha" in data["warnings"][0]


def test_the_drop_in_ws_renders_is_the_same_whatever_else_is_in_the_folder(box):
    pol = policy.load(box / "home" / ".ws" / "policy.toml")
    home = str(box / "home")
    before = {n: wallsettings.class_file_text(pol.classes[n], home, KIND) for n in ("alpha", "beta")}
    put(box, "alpha", "beta", "gamma")
    after = {n: wallsettings.class_file_text(pol.classes[n], home, KIND) for n in ("alpha", "beta")}
    assert before == after
