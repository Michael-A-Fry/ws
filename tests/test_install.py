"""The setup engine: plan is pure, apply writes only what differs, a second install reports 0 changes, uninstall
leaves no residue. Everything lives under a tmp HOME and WS_HOME."""

from __future__ import annotations

import hashlib
import json
import tomllib

import pytest

from ws import cli, install, osinfo


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


def test_uninstall_ignores_a_manifest_entry_whose_hash_does_not_match(box, capsys):
    victim = box / "precious.txt"
    victim.write_text("keep me")
    mp = install.manifest_path()
    mp.parent.mkdir(parents=True)
    entry = {"path": str(victim), "kind": "file", "sha256": "0" * 64, "created_by_ws": True}
    mp.write_text(json.dumps({"schema": 1, "ws_version": "x", "entries": [entry]}))
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "you changed it" in out and victim.read_text() == "keep me"


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
    assert {s.kind for s in steps} == {"file", "dir"}
    code, out, _ = run(capsys, "install", "--dry-run", "--runner", "opencode", "--notes", str(box / "Notes"))
    assert code == 0 and "would create" in out and "dry run: " in out
    assert snapshot(box) == pre


def test_json_result_counts(box, capsys):
    code, out, _ = run(capsys, "install", "--yes", "--json", "--notes", str(box / "Notes"))
    first = json.loads(out)
    assert code == 0 and first["created"] > 0 and first["changed"] == 0 and first["dry_run"] is False
    second = json.loads(run(capsys, "install", "--yes", "--json", "--notes", str(box / "Notes"))[1])
    assert second["created"] == second["changed"] == 0 and second["unchanged"] == len(second["steps"])


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
    checks = {c["id"]: c["status"] for c in json.loads(out)["checks"]}
    assert checks["WS003"] == checks["WS004"] == checks["WS106"] == "ok"
    assert checks["WS105"] in ("ok", "warn")  # memory exists; `ws memory init` makes it a git repo
