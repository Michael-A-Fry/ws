"""The install manifest keeps every record a run didn't write: an install that plans no gate merge leaves the earlier
merge record (and the hooks in the runner's config) alone, a newer ws's keys and fields come through a save, and the save
happens under the user-config lock. Everything lives under a tmp HOME and WS_HOME."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace

import pytest

from ws import cli, install, userconfig


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def do_install(capsys, box, *extra):
    return run(capsys, "install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes"), *extra)


def manifest_file(box):
    return box / ".ws" / "install-manifest.json"


def manifest(box):
    return json.loads(manifest_file(box).read_text(encoding="utf-8"))


def merge_records(box):
    return [e for e in manifest(box)["entries"] if e.get("kind") == "merge"]


def settings(box):
    return json.loads((box / ".claude" / "settings.json").read_text(encoding="utf-8"))


def without_a_runner(box):
    """The answers a setup screen builds on a machine where no runner was found."""
    return replace(install.answers_from_profile(os.environ), runner="")


def has_ws_hooks(box):
    return "PreToolUse" in settings(box).get("hooks", {})


def rewrite(box, edit):
    doc = manifest(box)
    edit(doc)
    manifest_file(box).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


# ---- the incident: an install that plans no gate merge -------------------------------------------------------------


def test_an_install_planned_without_a_runner_keeps_the_gate_merged(box, capsys):
    assert do_install(capsys, box)[0] == 0
    before = merge_records(box)
    assert len(before) == 1 and has_ws_hooks(box)

    steps = install.plan(without_a_runner(box), os.environ)
    assert any(s.kind == "merge" and s.carried for s in steps)
    result = install.apply(steps, os.environ)
    assert any("stays merged" in n for n in result.notes)

    assert merge_records(box) == before
    assert has_ws_hooks(box)


def test_steps_that_leave_the_merge_out_leave_its_record_as_it_was(box, capsys):
    do_install(capsys, box)
    before = manifest(box)["entries"]
    steps = [s for s in install.plan(without_a_runner(box), os.environ) if s.kind != "merge"]
    install.apply(steps, os.environ)
    assert manifest(box)["entries"] == before
    assert has_ws_hooks(box)


def test_a_plain_install_keeps_the_gate_and_says_so(box, capsys):
    do_install(capsys, box)
    code, out, _ = run(capsys, "install", "--yes", "--notes", str(box / "Notes"))
    assert code == 0 and "stays merged" in out and "--gate launch" in out
    assert len(merge_records(box)) == 1 and has_ws_hooks(box)


def test_a_profile_with_no_gate_setting_and_a_record_stays_every_session(box, capsys):
    do_install(capsys, box)
    prof = box / ".ws" / "profile.toml"
    prof.write_text(prof.read_text(encoding="utf-8").replace('gate = "every-session"\n', ""), encoding="utf-8")
    assert do_install(capsys, box)[0] == 0
    assert len(merge_records(box)) == 1 and has_ws_hooks(box)


def test_only_gate_launch_takes_the_gate_out(box, capsys):
    (box / ".claude").mkdir()
    (box / ".claude" / "settings.json").write_text('{"model": "opus"}\n', encoding="utf-8")
    do_install(capsys, box)
    assert has_ws_hooks(box)
    assert do_install(capsys, box, "--gate", "launch")[0] == 0
    assert merge_records(box) == []
    assert not has_ws_hooks(box)


def test_a_setup_screen_run_does_not_undo_a_launch_choice(box, capsys):
    do_install(capsys, box, "--gate", "launch")
    steps = install.plan(without_a_runner(box), os.environ)
    assert not any(s.kind == "merge" for s in steps)


def test_a_merge_that_cannot_be_rebuilt_is_left_as_it_is(box, capsys, monkeypatch):
    do_install(capsys, box)
    before = merge_records(box)

    def refuse(*a, **k):
        raise install.WsError("refused", "no stable interpreter")

    monkeypatch.setattr(userconfig, "plan", refuse)
    steps = install.plan(without_a_runner(box), os.environ)
    assert not any(s.kind == "merge" for s in steps)
    install.apply(steps, os.environ)
    assert merge_records(box) == before and has_ws_hooks(box)


# ---- a newer ws's data comes through ----------------------------------------------------------------------------------


def test_unknown_keys_and_fields_come_through_a_save(box, capsys):
    do_install(capsys, box)

    def edit(doc):
        doc["future_key"] = {"a": [1, 2]}
        doc["schema"] = 7
        doc["entries"].append({"note": "from a newer ws"})  # no path: not usable here, carried
        for e in doc["entries"]:
            if e.get("kind") == "merge":
                e["newer_field"] = "m"
            elif e.get("kind") == "file" and e["path"].endswith("profile.toml"):
                e["newer_field"] = "f"

    rewrite(box, edit)
    prof = box / ".ws" / "profile.toml"
    prof.write_text(prof.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert do_install(capsys, box, "--cap", "30")[0] == 0  # the profile changes, so ws writes and records it again

    doc = manifest(box)
    assert doc["future_key"] == {"a": [1, 2]} and doc["schema"] == 7
    assert {"note": "from a newer ws"} in doc["entries"]
    assert merge_records(box)[0]["newer_field"] == "m"
    assert next(e for e in doc["entries"] if e.get("path", "").endswith("profile.toml"))["newer_field"] == "f"


def test_a_re_merge_keeps_a_newer_field_on_the_merge_record(box, capsys):
    do_install(capsys, box)
    rewrite(box, lambda doc: [e.update(newer_field=1) for e in doc["entries"] if e.get("kind") == "merge"])
    pol = box / ".ws" / "policy.toml"
    pol.write_text(pol.read_text(encoding="utf-8").replace("deny_tools = []", 'deny_tools = ["web_fetch"]'))
    assert do_install(capsys, box)[0] == 0
    rec = merge_records(box)[0]
    assert rec["newer_field"] == 1 and rec["wanted"] != ""


def test_forget_drops_only_what_it_is_told_and_keeps_the_rest(box, capsys):
    do_install(capsys, box)
    rewrite(box, lambda doc: doc.update(future_key=True))
    gone = box / ".ws" / "gate" / "claude-code" / "settings.json"
    install.forget([gone], os.environ)
    doc = manifest(box)
    assert doc["future_key"] is True
    assert str(gone) not in [e.get("path") for e in doc["entries"]]
    assert len(merge_records(box)) == 1


def test_a_record_another_run_adds_meanwhile_survives_this_run_s_save(box, capsys):
    do_install(capsys, box)
    other = {"path": str(box / ".ws" / "elsewhere.txt"), "kind": "file", "sha256": "0" * 64, "created_by_ws": True}
    fired = []

    def meanwhile(step, action):
        if not fired:
            fired.append(1)
            rewrite(box, lambda doc: doc["entries"].append(other))

    pol = box / ".ws" / "policy.toml"
    pol.write_text(pol.read_text(encoding="utf-8").replace("deny_tools = []", 'deny_tools = ["web_fetch"]'))
    steps = install.plan(install.answers_from_profile(os.environ), os.environ)
    install.apply(steps, os.environ, progress=meanwhile)
    assert other in manifest(box)["entries"]


# ---- the lock ---------------------------------------------------------------------------------------------------------


def test_the_manifest_is_saved_while_the_user_config_lock_is_held(box, capsys, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    lock = box / ".claude" / ".settings.json.ws-lock"
    seen: list[str] = []
    real = install._write_atomic

    def observe(path, data):
        if path.name == install.MANIFEST:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600) if lock.exists() else None
            if fd is None:
                seen.append("no lock file")
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("free")
                except BlockingIOError:
                    seen.append("held")
                finally:
                    os.close(fd)
        real(path, data)

    monkeypatch.setattr(install, "_write_atomic", observe)
    assert do_install(capsys, box)[0] == 0
    assert seen and seen[-1] == "held"
    assert not lock.exists()  # and it is let go afterwards


def test_the_lock_is_taken_before_the_save_and_let_go_after(box, capsys, monkeypatch):
    events: list[str] = []
    real_locked, real_commit = userconfig.locked, install._commit

    class Spy:
        def __init__(self, path):
            self.inner = real_locked(path)

        def __enter__(self):
            events.append("lock")
            return self.inner.__enter__()

        def __exit__(self, *a):
            events.append("unlock")
            return self.inner.__exit__(*a)

    def commit(*a, **k):
        events.append("save")
        return real_commit(*a, **k)

    monkeypatch.setattr(userconfig, "locked", Spy)
    monkeypatch.setattr(install, "_commit", commit)
    assert do_install(capsys, box)[0] == 0
    assert events == ["lock", "save", "unlock"]


def test_a_run_that_does_not_touch_the_user_config_takes_no_lock(box, capsys, monkeypatch):
    do_install(capsys, box)
    events: list[str] = []
    real = userconfig.locked
    monkeypatch.setattr(userconfig, "locked", lambda p: events.append("lock") or real(p))
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    assert events == []


# ---- the merge record says what the release wanted --------------------------------------------------------------------


def test_the_merge_record_holds_the_wanted_ops_beside_the_added_ones(box, capsys):
    do_install(capsys, box)
    rec = merge_records(box)[0]
    assert rec["wanted_ops"] and all(op in rec["wanted_ops"] for op in rec["added"])
    assert userconfig.wanted(rec["wanted_ops"]) == rec["wanted"]


def test_ws008_reports_an_op_a_newer_release_no_longer_asks_for(box, capsys):
    do_install(capsys, box)
    assert userconfig.check(os.environ)[0] == "ok"
    gone = {"op": "append", "path": ["hooks", "OldEvent"], "value": {"hooks": [{"type": "command", "command": "x"}]}}

    def edit(doc):
        for e in doc["entries"]:
            if e.get("kind") == "merge":
                e["wanted_ops"] = [*e["wanted_ops"], gone]
                e["wanted"] = userconfig.wanted(e["wanted_ops"])

    rewrite(box, edit)
    status, detail, fix = userconfig.check(os.environ)
    assert status == "warn" and "no longer asks for 1" in detail and "hooks.OldEvent" in detail
    assert "ws install" in fix
    assert do_install(capsys, box)[0] == 0
    assert userconfig.check(os.environ)[0] == "ok"


def test_ws008_with_a_record_from_before_wanted_ops_reports_no_drop(box, capsys):
    do_install(capsys, box)
    rewrite(box, lambda doc: [e.pop("wanted_ops", None) for e in doc["entries"]])
    assert userconfig.check(os.environ)[0] == "ok"


# ---- a file that already holds the released text -----------------------------------------------------------------------


def one_file(box, text):
    return install.Step(box / ".ws" / "jobs" / "unit.txt", "file", text, "a test file")


def sha_of(box, path):
    return next(e["sha256"] for e in manifest(box)["entries"] if e["path"] == str(path))


def test_a_file_that_holds_exactly_the_planned_text_is_unchanged_and_its_hash_is_refreshed(box):
    step = one_file(box, "released text\n")
    install.apply([step], os.environ)
    rewrite(box, lambda doc: [e.update(sha256="a" * 64) for e in doc["entries"] if e["path"] == str(step.path)])
    result = install.apply([step], os.environ)
    assert result.unchanged == [step.path] and not result.edited and result.changes == 0
    assert sha_of(box, step.path) == hashlib.sha256(b"released text\n").hexdigest()


def test_a_real_edit_still_counts_as_edited_and_is_left(box):
    step = one_file(box, "released text\n")
    install.apply([step], os.environ)
    rewrite(box, lambda doc: [e.update(sha256="a" * 64) for e in doc["entries"] if e["path"] == str(step.path)])
    step.path.write_text("my own words\n", encoding="utf-8")
    result = install.apply([step], os.environ)
    assert result.edited == [step.path] and result.changes == 0
    assert step.path.read_text(encoding="utf-8") == "my own words\n"
    assert sha_of(box, step.path) == "a" * 64
