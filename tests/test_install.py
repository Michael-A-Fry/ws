"""The setup engine: plan is pure, apply writes only what differs, a second install reports 0 changes, uninstall
leaves no residue. Everything lives under a tmp HOME and WS_HOME."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

from ws import cli, install, osinfo, policy, selfcmd, wallsettings


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def snapshot(root) -> dict[str, str]:
    """Every path under root with a file's hash (or 'dir')."""
    out = {}
    for p in sorted(root.rglob("*")):
        key = str(p.relative_to(root))
        out[key] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


FULL = ["--yes", "--runner", "claude-code", "--model", "some-model", "--cap", "20", "--stays-on", "yes"]


def test_a_second_install_reports_0_changes_and_uninstall_leaves_no_residue(box, capsys):
    pre = snapshot(box)
    notes = box / "Notes"
    code, out, _ = run(capsys, "install", *FULL, "--notes", str(notes))
    assert code == 0 and "created" in out and "0 changes" not in out
    after_first = snapshot(box)
    assert after_first != pre

    code, out, _ = run(capsys, "install", *FULL, "--notes", str(notes))
    assert code == 0 and "0 changes" in out
    assert snapshot(box) == after_first

    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "left 0" in out
    assert snapshot(box) == pre


@pytest.fixture
def xdg_box(tmp_path, monkeypatch):
    """A HOME with no WS_HOME and no ~/.ws: ws uses the XDG folders."""
    home = tmp_path / "xdg-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WS_HOME", raising=False)
    monkeypatch.chdir(home)
    return home


def test_in_the_xdg_layout_each_file_lands_in_its_folder_and_uninstall_leaves_no_residue(xdg_box, capsys):
    box = xdg_box
    pre = snapshot(box)
    notes = box / "Notes"
    code, out, _ = run(capsys, "install", *FULL, "--notes", str(notes))
    assert code == 0 and "0 changes" not in out
    cfg, state = box / ".config" / "ws", box / ".local" / "state" / "ws"
    assert (cfg / "policy.toml").is_file() and (cfg / "profile.toml").is_file()
    assert (state / "install-manifest.json").is_file() and (state / "gate" / "claude-code" / "settings.json").is_file()
    assert not (box / ".ws").exists()
    after_first = snapshot(box)

    code, out, _ = run(capsys, "install", *FULL, "--notes", str(notes))
    assert code == 0 and "0 changes" in out and snapshot(box) == after_first

    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "left 0" in out
    assert snapshot(box) == pre


def test_in_the_old_layout_install_keeps_using_the_old_folder(xdg_box, capsys):
    (xdg_box / ".ws").mkdir()
    assert run(capsys, "install", *FULL, "--notes", str(xdg_box / "Notes"))[0] == 0
    assert (xdg_box / ".ws" / "policy.toml").is_file() and not (xdg_box / ".config").exists()


def test_uninstall_removes_empty_run_folders_in_the_work_folder_and_keeps_what_a_job_wrote(xdg_box, capsys):
    assert run(capsys, "install", *FULL, "--notes", str(xdg_box / "Notes"))[0] == 0
    root = xdg_box / ".local" / "state" / "ws-work"
    full, empty = root / "20261031T120000Z-aaaaaa", root / "20261031T120001Z-bbbbbb"
    full.mkdir(parents=True)
    empty.mkdir()
    (full / "hello.txt").write_text("hello", encoding="utf-8")
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and (full / "hello.txt").read_text(encoding="utf-8") == "hello" and not empty.exists()
    assert f"has your files in it, so ws left it: {full}" in out
    (full / "hello.txt").unlink()
    full.rmdir()
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert not root.exists()  # nothing left in it: the folder goes too


def test_uninstall_keeps_a_notes_folder_that_was_there_and_what_is_in_it(box, capsys):
    notes = box / "Notes"
    notes.mkdir()
    (notes / "mine.md").write_text("my note")
    pre = snapshot(box)
    assert run(capsys, "install", "--yes", "--notes", str(notes))[0] == 0
    assert (notes / "_inbox").is_dir()
    (notes / "_inbox" / "captured.md").write_text("a note ws captured")
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "has your files in it" in out
    assert (notes / "mine.md").read_text() == "my note"
    assert (notes / "_inbox" / "captured.md").is_file()
    assert not (box / ".ws").exists() and not (box / "ws-memory").exists()
    assert set(pre) <= set(snapshot(box))


def test_a_hand_edited_file_is_left_and_listed(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    policy = box / ".ws" / "policy.toml"
    policy.write_text(policy.read_text() + "\n# mine\n")
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0
    assert f"you changed it, so ws left it: {policy}" in out
    assert policy.read_text().endswith("# mine\n")
    assert not (box / ".ws" / "profile.toml").exists()
    assert not (box / ".ws" / install.MANIFEST).exists()


def test_uninstall_never_touches_a_path_not_in_the_manifest(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    stray = box / ".ws" / "mine.txt"
    stray.write_text("x")
    other = box / "ws-memory" / "a-memory.md"
    other.write_text("y")
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert stray.read_text() == "x" and other.read_text() == "y"


def test_uninstall_leaves_an_entry_in_ws_home_whose_hash_does_not_match(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    mp = install.manifest_path()
    doc = json.loads(mp.read_text())
    victim = box / ".ws" / "precious.txt"
    victim.write_text("keep me")
    doc["entries"].append({"path": str(victim), "kind": "file", "sha256": "0" * 64, "created_by_ws": True})
    mp.write_text(json.dumps(doc))
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and f"you changed it, so ws left it: {victim}" in out and victim.read_text() == "keep me"


def tamper(*entries, roots=None):
    """Add (path, kind) entries to the manifest, each with a hash that matches the file as it is now."""
    mp = install.manifest_path()
    doc = json.loads(mp.read_text())
    for path, kind in entries:
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else None
        doc["entries"].append({"path": str(path), "kind": kind, "sha256": digest, "created_by_ws": True})
    if roots is not None:
        doc["roots"] = roots
    mp.write_text(json.dumps(doc))


def test_an_edited_manifest_cannot_point_uninstall_at_your_other_files(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    key = box / ".ssh" / "id_rsa"
    key.parent.mkdir()
    key.write_text("private")
    empty = box / "somewhere-else"
    empty.mkdir()
    sneaky = box / ".ws" / ".." / ".ssh" / "id_rsa"  # looks like it is under ws's home until the .. is resolved
    tamper((key, "file"), (sneaky, "file"), (empty, "dir"))
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0
    assert key.read_text() == "private" and empty.is_dir()
    assert "outside the folders ws manages" in out and str(key) in out
    assert not (box / ".ws").exists()  # what ws really made still went


def test_widening_the_recorded_roots_does_not_help(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    key = box / ".ssh" / "id_rsa"
    key.parent.mkdir()
    key.write_text("private")
    tamper((key, "file"), roots=[str(box), "/"])
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert key.read_text() == "private"


def test_an_entry_behind_a_symlinked_folder_is_refused(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    outside = box / "outside"
    outside.mkdir()
    target = outside / "x.txt"
    target.write_text("not ws's")
    (box / ".ws" / "link").symlink_to(outside, target_is_directory=True)
    tamper((box / ".ws" / "link" / "x.txt", "file"))
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert target.read_text() == "not ws's"


def test_the_manifest_records_the_folders_ws_manages(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    roots = json.loads(install.manifest_path().read_text())["roots"]
    assert roots == sorted([str(box / ".ws"), str(box / "Notes"), str(box / "ws-memory")])


def test_a_notes_folder_that_is_your_whole_home_is_refused(box, capsys):
    pre = snapshot(box)
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box))
    assert code == 1 and "too broad" in err and snapshot(box) == pre


def test_a_failure_part_way_still_records_what_was_created(box, capsys, monkeypatch):
    real = install._write_atomic

    def flaky(path, data):
        if path.name == "profile.toml":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(install, "_write_atomic", flaky)
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"))
    assert code == 1 and "disk full" in err
    assert (box / ".ws" / "policy.toml").is_file()
    recorded = {e["path"] for e in json.loads(install.manifest_path().read_text())["entries"]}
    assert str(box / ".ws" / "policy.toml") in recorded and str(box / ".ws") in recorded
    monkeypatch.setattr(install, "_write_atomic", real)
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert not (box / ".ws").exists()


def test_a_generated_file_you_edited_is_left_and_reported_not_overwritten(box, capsys):
    args = ["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes")]
    assert run(capsys, *args)[0] == 0
    gate = next(p for p in (box / ".ws" / "gate").rglob("*") if p.is_file())
    gate.write_text(gate.read_text() + "\n")
    mine = gate.read_text()
    code, out, _ = run(capsys, *args)
    assert code == 0 and gate.read_text() == mine
    assert f"you changed it, so ws left it: {gate}" in out and "0 changes" in out and "1 left" in out
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert gate.read_text() == mine


def test_profile_comments_survive_a_rerun_that_changes_nothing(box, capsys):
    args = ["install", "--yes", "--notes", str(box / "Notes")]
    assert run(capsys, *args)[0] == 0
    prof = box / ".ws" / "profile.toml"
    prof.write_text(prof.read_text() + "\n# my own comment\n")
    code, out, _ = run(capsys, *args)
    assert code == 0 and "0 changes" in out and "my own comment" in prof.read_text()


def test_a_profile_you_edited_is_kept_not_deleted_when_ws_changes_it_or_uninstalls(box, capsys):
    args = ["install", "--yes", "--notes", str(box / "Notes")]
    assert run(capsys, *args)[0] == 0
    prof = box / ".ws" / "profile.toml"
    prof.write_text(prof.read_text() + '\n[machine.extra]\nmine = "yes"\n')
    code, out, _ = run(capsys, *args, "--cap", "7")
    assert code == 0 and "1 changed" in out and "had your edits" in out
    kept = tomllib.loads(prof.read_text())
    assert kept["machine"]["extra"] == {"mine": "yes"} and kept["roles"]["budget"]["monthly_cap_usd"] == 7
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert prof.is_file()


def test_uninstall_leaves_a_ws_home_that_was_there_before(box, capsys):
    (box / ".ws").mkdir()
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert (box / ".ws").is_dir() and list((box / ".ws").iterdir()) == []


@pytest.mark.parametrize("cap", ["nan", "inf", "-inf", "0"])
def test_a_cap_that_is_not_a_positive_number_is_refused(box, capsys, cap):
    pre = snapshot(box)
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"), f"--cap={cap}")
    assert code == 1 and "above 0" in err and "Traceback" not in err and snapshot(box) == pre


def test_a_rewrite_keeps_the_files_mode_and_leaves_no_temp_files(box, capsys):
    args = ["install", "--yes", "--notes", str(box / "Notes")]
    assert run(capsys, *args)[0] == 0
    prof = box / ".ws" / "profile.toml"
    prof.chmod(0o600)
    assert run(capsys, *args, "--cap", "3")[0] == 0
    assert prof.stat().st_mode & 0o777 == 0o600
    assert not [p for p in (box / ".ws").iterdir() if p.name.endswith(".ws-tmp")]


def test_a_dry_run_counts_the_same_as_the_real_run_when_a_folder_follows_its_contents(box):
    top = box / "made" / "inner"
    steps = [install.Step(top / "a.txt", "file", "x"), install.Step(top, "dir")]
    dry = install.apply(steps, dry_run=True)
    assert not (box / "made").exists()
    real = install.apply(steps)
    assert (dry.created, dry.changed, dry.unchanged) == (real.created, real.changed, real.unchanged)
    assert real.created == [top / "a.txt"] and real.unchanged == [top]


def test_a_file_ws_changed_but_did_not_create_is_left_on_uninstall(box, capsys):
    notes = box / "Notes"
    assert run(capsys, "init", "--yes", "--notes", str(notes), "--create-notes")[0] == 0
    before = (box / ".ws" / "profile.toml").read_text()
    assert run(capsys, "install", "--yes", "--notes", str(notes))[0] == 0
    assert (box / ".ws" / "profile.toml").read_text() != before
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "ws did not create it" in out
    assert (box / ".ws" / "profile.toml").is_file()


def test_plan_is_pure_and_dry_run_writes_nothing(box, capsys):
    pre = snapshot(box)
    steps = install.plan(install.Answers(notes=str(box / "Notes"), runner="opencode"))
    assert snapshot(box) == pre
    assert {s.kind for s in steps} == {"file", "dir", "merge"}
    code, out, _ = run(capsys, "install", "--dry-run", "--runner", "opencode", "--notes", str(box / "Notes"))
    assert code == 0 and "would create" in out and "dry run: " in out
    assert snapshot(box) == pre


def test_json_result_counts(box, capsys):
    code, out, _ = run(capsys, "install", "--yes", "--json", "--notes", str(box / "Notes"))
    first = json.loads(out)["data"]
    counts = first["counts"]
    assert code == 0 and counts["created"] > 0 and counts["changed"] == 0 and first["dry_run"] is False
    assert first["first"] == "ws doctor" and len(first["created"]) == counts["created"]
    second = json.loads(run(capsys, "install", "--yes", "--json", "--notes", str(box / "Notes"))[1])["data"]
    assert second["counts"]["created"] == second["counts"]["changed"] == 0
    assert second["counts"]["unchanged"] == len(second["steps"]) == len(second["skipped"])
    assert {a["status"] for a in second["artifacts"]} == {"skipped"}


def test_install_json_artefact_statuses(box, capsys):
    notes = str(box / "Notes")
    dry = json.loads(run(capsys, "install", "--dry-run", "--json", "--notes", notes)[1])["data"]
    assert dry["deferred"] and not dry["created"] and not dry["updated"]
    done = json.loads(run(capsys, "install", "--yes", "--json", "--notes", notes)[1])["data"]
    assert done["created"] and not done["deferred"]
    assert {a["status"] for a in done["artifacts"]} <= {"created", "updated", "skipped", "left"}


def test_profile_gets_the_answers_and_keeps_what_was_there(box, capsys):
    p = box / ".ws" / "profile.toml"
    p.parent.mkdir()
    p.write_text('[machine]\nnickname = "desk"\n\n[roles.budget]\nmonthly_cap_usd = 5\nnote = "mine"\n')
    code, *_ = run(capsys, "install", *FULL, "--notes", str(box / "Notes"), "--server", "lab.example.com")
    assert code == 0
    prof = tomllib.loads(p.read_text())
    assert prof["machine"]["nickname"] == "desk"
    assert prof["machine"]["server"] == "lab.example.com" and prof["machine"]["stays_on"] is True
    assert prof["roles"]["budget"] == {"monthly_cap_usd": 20, "note": "mine"}
    assert prof["roles"]["runner"]["adapter"] == "claude-code"
    assert prof["roles"]["model"]["default"] == "some-model"
    assert prof["roles"]["notes"]["path"] == str(box / "Notes")
    assert prof["roles"]["memory"]["path"] == "~/ws-memory"
    assert prof["modules"] == {
        "memory": True,
        "notes": True,
        "skills": True,
        "mail-drafts": False,
        "calendar": False,
        "finance": False,
        "knowledge-graph": False,
    }


def test_a_rerun_with_no_flags_keeps_the_earlier_answers(box, capsys):
    assert run(capsys, "install", *FULL, "--notes", str(box / "Notes"))[0] == 0
    before = snapshot(box)
    code, out, _ = run(capsys, "install", "--yes")
    assert code == 0 and "0 changes" in out and snapshot(box) == before


def test_an_existing_policy_is_kept_not_replaced(box, capsys):
    pol = box / ".ws" / "policy.toml"
    pol.parent.mkdir()
    from ws import policy

    text = policy.default_toml() + "\n# my own note\n"
    pol.write_text(text)
    assert run(capsys, "install", "--yes", "--runner", "opencode", "--notes", str(box / "Notes"))[0] == 0
    assert pol.read_text() == text
    assert (box / ".ws" / "gate" / "opencode").is_dir()


def test_the_gate_files_come_from_the_generator_for_each_runner(box, capsys):
    from ws import adapters, policy

    for name in adapters.all_adapters():
        assert run(capsys, "install", "--yes", "--runner", name, "--notes", str(box / "Notes"))[0] == 0
        out = box / ".ws" / "gate" / name
        want = adapters.get(name).generate(
            policy.default_policy(), ["ws"], out, (box / ".ws" / "policy.toml").resolve()
        )
        assert {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} == set(want)


def test_the_manifest_records_what_ws_created(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    doc = json.loads(install.manifest_path().read_text())
    assert doc["schema"] == 1 and doc["ws_version"]
    by_path = {e["path"]: e for e in doc["entries"]}
    prof = by_path[str(box / ".ws" / "profile.toml")]
    assert prof["kind"] == "file" and prof["created_by_ws"] is True
    assert prof["sha256"] == hashlib.sha256((box / ".ws" / "profile.toml").read_bytes()).hexdigest()
    assert by_path[str(box / "Notes" / "_inbox")]["kind"] == "dir"
    assert str(box / ".ws") in by_path  # ws made its own home, so it records it


def test_a_changed_file_updates_its_hash_in_the_manifest(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, out, _ = run(capsys, "install", "--yes", "--notes", str(box / "Notes"), "--cap", "9")
    assert code == 0 and "1 changed" in out
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert not (box / ".ws").exists()


def test_uninstall_dry_run_removes_nothing(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    before = snapshot(box)
    code, out, _ = run(capsys, "uninstall", "--dry-run")
    assert code == 0 and "would remove" in out and "left 0" in out
    assert snapshot(box) == before


def test_uninstall_without_yes_needs_a_terminal(box, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, _, err = run(capsys, "uninstall")
    assert code == 2 and "--yes" in err and (box / ".ws").is_dir()


def test_uninstall_with_nothing_installed_is_a_no_op(box, capsys):
    pre = snapshot(box)
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "removed 0" in out and snapshot(box) == pre


def test_keep_data_false_is_not_built(box):
    with pytest.raises(install.InstallError, match="not built"):
        install.uninstall(keep_data=False)


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--cap", "-3"], "above 0"),
        (["--module", "nope"], "unknown module"),
        (["--stays-on", "maybe"], "yes or no"),
    ],
)
def test_bad_answers_are_refused_without_writing(box, capsys, argv, message):
    pre = snapshot(box)
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"), *argv)
    assert code == 1 and message in err and snapshot(box) == pre


def test_a_profile_that_does_not_parse_is_reported(box, capsys):
    p = box / ".ws" / "profile.toml"
    p.parent.mkdir()
    p.write_text("not = = toml\n")
    code, _, err = run(capsys, "install", "--yes")
    assert code == 1 and "profile.toml" in err and p.read_text() == "not = = toml\n"


def test_a_file_where_a_folder_should_be_is_reported(box, capsys):
    (box / "Notes").write_text("a file")
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"))
    assert code == 1 and "in the way" in err


def test_native_windows_is_refused(box):
    osi = osinfo.OSInfo("windows", "11", "x86_64", "none", False)
    with pytest.raises(install.InstallError, match="WSL2"):
        install.plan(install.Answers(), osi=osi)


def test_a_manifest_that_is_not_ours_stops_everything(box, capsys):
    mp = install.manifest_path()
    mp.parent.mkdir()
    mp.write_text("[1, 2]")
    code, _, err = run(capsys, "uninstall", "--yes")
    assert code == 1 and "manifest" in err


def test_doctor_is_happy_with_what_install_wrote(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    _, out, _ = run(capsys, "doctor", "--json", "--offline")
    checks = {c["id"]: c["status"] for c in json.loads(out)["data"]["checks"]}
    assert checks["WS003"] == checks["WS004"] == checks["WS106"] == "ok"
    assert checks["WS105"] in ("ok", "warn")  # memory exists; `ws memory init` makes it a git repo


def test_roots_written_into_the_manifest_cannot_authorise_anything(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    key = box / ".ssh" / "id_rsa"
    key.parent.mkdir()
    key.write_text("private")
    tamper((key, "file"), roots=[str(box / ".ssh"), str(box / ".ws")])
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and key.read_text() == "private"
    assert "outside the folders ws manages" in out and str(key) in out


def test_uninstall_acts_in_the_notes_folder_the_profile_names_now(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    assert run(capsys, "install", "--yes", "--notes", str(box / "Other"))[0] == 0
    code, _, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and not (box / "Other").exists()
    assert (box / "Notes" / "_inbox").is_dir()  # the old folder is no longer one ws manages, so it is left


def test_a_failing_manifest_save_does_not_mask_the_first_error(box, capsys, monkeypatch):
    real = install._write_atomic

    def flaky(path, data):
        if path.name == "profile.toml":
            raise OSError("profile write failed")
        if path.name == install.MANIFEST:
            raise OSError("manifest write failed")
        real(path, data)

    monkeypatch.setattr(install, "_write_atomic", flaky)
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"))
    assert code == 1 and "profile write failed" in err and "manifest write failed" not in err


def test_a_temp_file_is_cleaned_up_on_any_exception(box, monkeypatch):
    target = box / "f.txt"

    def interrupted(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(install.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        install._write_atomic(target, b"x")
    assert list(box.iterdir()) == []


@pytest.mark.parametrize("where", ["machine", "roles"])
def test_a_profile_whose_notes_folder_is_your_home_is_one_clear_error(box, capsys, where):
    prof = box / ".ws" / "profile.toml"
    prof.parent.mkdir()
    prof.write_text('[machine]\nnotes = "~"\n' if where == "machine" else '[roles.notes]\npath = "~"\n')
    pre = snapshot(box)
    for argv in (["install", "--yes"], ["install", "--dry-run"]):
        code, _, err = run(capsys, *argv)
        assert code == 1 and "too broad" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1
    code, out, err = run(capsys, "install", "--yes", "--json")  # the same error, as the envelope on stdout
    env = json.loads(out)
    assert code == 1 and not err and env["status"] == "error" and env["error_kind"] == "failed"
    assert "too broad" in env["message"]
    assert snapshot(box) == pre
    code, _, err = run(capsys, "uninstall", "--yes")
    assert code == 0 and "Traceback" not in err
    assert run(capsys, "module", "list")[0] == 0


def test_uninstall_removes_only_empty_run_folders_and_keeps_what_a_job_wrote(box, capsys, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert run(capsys, "install", *FULL, "--notes", str(box / "Notes"))[0] == 0
    root = box / ".local" / "state" / "ws"
    full, empty = root / "work" / "20261031T120000Z-aaaaaa", root / "work" / "20261031T120001Z-bbbbbb"
    full.mkdir(parents=True)
    empty.mkdir()
    (full / "hello.txt").write_text("hello", encoding="utf-8")
    code, out, _ = run(capsys, "uninstall", "--dry-run")
    assert code == 0 and empty.is_dir() and "would remove" in out  # a dry run removes nothing
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0
    assert (full / "hello.txt").read_text(encoding="utf-8") == "hello"  # the job's output is the person's
    assert not empty.exists()
    assert f"has your files in it, so ws left it: {full}" in out
    (full / "hello.txt").unlink()
    full.rmdir()
    assert run(capsys, "uninstall", "--yes")[0] == 0  # nothing left in them: work/ and ws/ go too
    assert not root.exists() and (box / ".local" / "state").is_dir()


def test_uninstall_leaves_something_else_of_yours_in_the_work_root(box, capsys, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert run(capsys, "install", *FULL, "--notes", str(box / "Notes"))[0] == 0
    root = box / ".local" / "state" / "ws"
    (root / "work").mkdir(parents=True)
    (root / "mine.txt").write_text("mine", encoding="utf-8")
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and (root / "mine.txt").is_file() and not (root / "work").exists()
    assert f"has your files in it, so ws left it: {root}" in out


def test_uninstall_leaves_a_linked_work_root_alone(box, capsys, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert run(capsys, "install", *FULL, "--notes", str(box / "Notes"))[0] == 0
    elsewhere = box / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep", encoding="utf-8")
    (box / ".local" / "state").mkdir(parents=True)
    (box / ".local" / "state" / "ws").symlink_to(elsewhere)
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert (elsewhere / "keep.txt").is_file()


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_notes_value_counts_as_not_given_so_the_profile_folder_stays(box, capsys, blank):
    notes = box / "Notes"
    assert run(capsys, "install", *FULL, "--notes", str(notes))[0] == 0
    after_first = snapshot(box)
    code, out, _ = run(capsys, "install", *FULL, "--notes", blank)
    assert code == 0 and "0 changes" in out
    assert snapshot(box) == after_first


def test_the_hooks_of_a_launch_install_start_ws_with_an_absolute_isolated_interpreter(xdg_box, capsys, monkeypatch):
    """Not `ws` from PATH: a project's own PATH or PYTHONPATH could put another program in its place."""
    monkeypatch.setenv("PATH", f"{xdg_box}/planted:/usr/bin:/bin")
    code, _, _ = run(capsys, "install", *FULL, "--gate", "launch", "--notes", str(xdg_box / "Notes"))
    assert code == 0
    settings = json.loads((xdg_box / ".local" / "state" / "ws" / "gate" / "claude-code" / "settings.json").read_text())
    commands = [h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]]
    ws_commands = [c for c in commands if " gate " in c or " notify " in c or " session " in c]
    assert ws_commands
    prefix = shlex.join(selfcmd.argv())
    client = shlex.join([selfcmd.argv()[0], "-I", "-S"])
    for c in ws_commands:
        if " gate " in c:  # through the thin client: this Python, isolated, and the command it falls back to
            assert c.startswith(client + " ") and f" -- {prefix} gate " in c, c
        else:
            assert c.startswith(prefix + " "), c
    assert all(shlex.split(c)[0].startswith("/") and shlex.split(c)[1] == "-I" for c in ws_commands)


# --- ws install --class NAME: prints, writes nothing --------------------------------------------------------------------

CLASS_POLICY = """
[classes.work]
access = "walled"
os_user = "ws-work"
repos = ["/srv/work/**", "/srv/more"]

[classes.home]
access = "full"
repos = ["/srv/home/**"]
"""


def class_box(box, text: str = CLASS_POLICY):
    pol = box / ".ws" / "policy.toml"
    pol.parent.mkdir(exist_ok=True)
    pol.write_text(text, encoding="utf-8")
    return pol


def test_install_class_prints_commands_and_root_file_and_changes_nothing(box, capsys, monkeypatch):
    class_box(box)
    pre = snapshot(box)

    def no_process(*a, **k):
        raise AssertionError("ws must not start a process (no useradd, no sudo)")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    code, out, _ = run(capsys, "install", "--class", "work")
    assert code == 0
    target = wallsettings.class_file_path("work", osinfo.detect().kind)
    assert "sudo" in out and "ws-work" in out and str(target) in out
    assert f"sudo chown root:{'wheel' if osinfo.detect().kind == 'macos' else 'root'} {target}" in out
    assert "applies to every account on this machine" in out
    body = out.split("<<'WS_CLASS_FILE'\n", 1)[1].split("\nWS_CLASS_FILE", 1)[0]
    conf = json.loads(body)
    sb = conf["sandbox"]
    assert conf["permissions"] == {wallsettings.BYPASS_KEY: wallsettings.BYPASS_OFF, wallsettings.WRITE_BLOCK_KEY: True}
    assert not wallsettings.tokens_missing(conf)  # C360: a bare run as the class account keeps the tokens from commands
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["filesystem"]["allowWrite"][:2] == ["/srv/work", "/srv/more"] and "/tmp" in sb["filesystem"]["allowWrite"]
    assert sb["filesystem"]["denyRead"] == ["~/"] and sb["filesystem"]["allowRead"] == ["/srv/work", "/srv/more"]
    assert conf["env"] == {"WS_INBOUND": "lead-only"}
    assert snapshot(box) == pre  # nothing written, anywhere under the home
    assert not target.exists() and not target.parent.exists()


@pytest.mark.parametrize("repo", ["/srv/work/ops-*", "/srv/work/*", "~/code/*", "/srv/*/work/**", "/srv/work/ops-?"])
def test_install_class_refuses_a_wildcard_in_a_folder_name(box, capsys, repo):
    """A glob like `work/ops-*` would put the folder above the repos in the file and the chown; it is a usage error."""
    class_box(box, f'[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["{repo}"]\n')
    pre = snapshot(box)
    code = cli.main(["install", "--class", "work", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert code == 2 and err["error_kind"] == "usage" and "wildcard in a folder name" in err["message"]
    assert "sudo" not in json.dumps(err) and snapshot(box) == pre


def test_install_class_holds_a_trailing_double_star_as_its_folder_and_nothing_wider(box, capsys):
    class_box(
        box, '[classes.work]\naccess = "walled"\nos_user = "ws-work"\nrepos = ["/srv/work/ops/**", "/srv/other"]\n'
    )
    code, out, _ = run(capsys, "install", "--class", "work")
    assert code == 0
    assert "chown -R ws-work /srv/work/ops\n" in out + "\n" and "chown -R ws-work /srv/other" in out
    assert "chown -R ws-work /srv/work\n" not in out + "\n" and "chown -R ws-work /srv\n" not in out + "\n"


def test_install_class_names_the_macos_paths_from_the_constants(box):
    pol = policy.loads(CLASS_POLICY)
    text = wallsettings.install_text(pol.classes["work"], {"HOME": str(box)}, "macos")
    mac = wallsettings.MANAGED_DIRS[-1]
    assert f"{mac}/{wallsettings.class_file_name('work')}" in text and "root:wheel" in text and "sysadminctl" in text


def test_install_class_json_says_it_writes_nothing(box, capsys):
    class_box(box)
    code = cli.main(["install", "--class", "work", "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert code == 0 and data["writes"] is False and data["class"] == "work" and "sudo" in data["text"]


def test_install_class_rejects_a_class_without_os_user(box, capsys):
    class_box(box)
    pre = snapshot(box)
    code = cli.main(["install", "--class", "home", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert code == 2 and err["error_kind"] == "usage" and "os_user" in err["message"] + err["hint"]
    code = cli.main(["install", "--class", "nothing", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert code == 2 and err["error_kind"] == "usage" and "work" in err["hint"]  # names the classes that can be used
    code = cli.main(["install", "--class", "work", "--runner", "claude-code", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert code == 2 and err["error_kind"] == "usage" and "--runner" in err["message"]
    assert snapshot(box) == pre


def test_install_without_class_never_renders_or_places_a_managed_file(box, capsys):
    class_box(box)
    assert run(capsys, "install", *FULL, "--notes", str(box / "Notes"))[0] == 0
    managed = Path(wallsettings.MANAGED_DIRS[0])
    assert not managed.exists() and not Path(wallsettings.MANAGED_FILES[0]).exists()
    assert not list(box.rglob("*ws-class-*"))


def test_install_class_places_the_file_under_another_name_and_moves_it(box, capsys):
    """A paste that stops half way must leave no half-written `*.json` in the folder Claude Code reads."""
    class_box(box)
    code, out, _ = run(capsys, "install", "--class", "work")
    assert code == 0
    target = wallsettings.class_file_path("work", osinfo.detect().kind)
    staged = f"{target}.tmp"
    assert f"sudo tee {staged} >/dev/null" in out and f"sudo chmod 644 {staged}" in out
    assert f"sudo mv {staged} {target}" in out
    assert f"sudo tee {target} " not in out  # the final name is only ever reached by the move
    assert out.index(f"sudo chmod 644 {staged}") < out.index("sudo mv ")


def test_install_class_gives_the_account_its_own_ws_policy_and_login(box, capsys):
    class_box(box)
    code, out, _ = run(capsys, "install", "--class", "work")
    assert code == 0
    step = out.split("5. ", 1)[1].split("6. ", 1)[0]
    assert "sudo -u ws-work -i" in step and "install.sh" in step and "ws setup" in step
    assert "[classes.work]" in step and "claude" in step and "sign in" in step
