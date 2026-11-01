"""The shipped skills: well-formed Agent Skills, scrubbed of anything personal, and installed (and removed) through the
install manifest without ever overwriting a skill the person already has."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re

import pytest

from ws import cli, frontmatter, skills

ADOPTED = skills.ADOPTED
NAMES = ["align", "checkpoint", "taste-gate", "dataviz", "resume", "ship"]

# Nothing in a shipped skill may name a person, a machine, a private path or a private tool.
SCRUB = {
    "a personal name": r"Michael|Fry\b",
    "an e-mail address": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "the private workspace": r"AI_Workspace|\.ws-live|ws dispatch|\.claude-state|ws-checkpoint|ws-test",
    "a lesson id": r"\bL-0\d\d\b",
    "a private agent": r"ui-density-critic",
    "a runner-only tool name": r"AskUserQuestion|TodoWrite|\bTask tool\b|ToolSearch",
    "a home path": r"~/\.claude|~/\.config|/home/\w+|/Users/\w+",
    "a decision id": r"DECISIONS #\d+",
}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    for var in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(home)
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def snapshot(root) -> dict[str, str]:
    return {
        str(p.relative_to(root)): "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
    }


def install(capsys, box, *extra):
    return run(capsys, "install", "--yes", "--notes", str(box / "Notes"), *extra)


# ---- the text ------------------------------------------------------------------------------------------------------


def test_the_six_skills_are_shipped():
    assert sorted(skills.SKILLS) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_skill_is_an_agent_skill(name):
    text = skills.SKILLS[name]
    doc = frontmatter.parse(text)
    assert doc.clean and text.startswith("---\n")
    assert set(doc.meta) == {"name", "description"}  # only the fields both runners read
    assert doc.meta["name"] == name and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    assert 40 <= len(doc.meta["description"]) <= 1024
    assert doc.body.strip().startswith("# ")
    assert text.endswith("\n") and "\t" not in text and "\r" not in text


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(("what", "pattern"), SCRUB.items())
def test_no_skill_carries_anything_personal(name, what, pattern):
    hit = re.search(pattern, skills.SKILLS[name])
    assert not hit, f"{name} has {what}: {hit.group(0)!r}"


def test_the_scrub_patterns_do_catch_what_they_are_for():
    planted = {
        "a personal name": "Michael said so",
        "an e-mail address": "write to " + "someone" + "@" + "example.org",
        "the private workspace": "see ~/AI_Workspace/docs",
        "a lesson id": "as in L-024",
        "a runner-only tool name": "ask with AskUserQuestion",
        "a home path": "in ~/.claude/skills",
    }
    for what, text in planted.items():
        assert re.search(SCRUB[what], text), what


def test_dataviz_is_ours_and_short():
    text = skills.SKILLS["dataviz"]
    assert len(text.splitlines()) <= 150
    for needed in ("small multiples", "colour-blind", "light and dark", "Pre-flight checklist", "right-align"):
        assert needed.lower() in text.lower()


def test_the_other_skills_stay_short():
    for name in ("align", "checkpoint", "taste-gate"):
        assert len(skills.SKILLS[name].splitlines()) <= 110


def test_resume_and_ship_are_short_and_say_what_they_do():
    for name, needed in (
        ("resume", ("STATE.md", "TODO.md", "git log", "two lines", "carry on")),
        ("ship", ("gh pr create", "exit code", "register", "commit", "branch")),
    ):
        text = skills.SKILLS[name]
        assert len(text.splitlines()) <= 120
        for word in needed:
            assert word.lower() in text.lower(), (name, word)
        assert not re.search(r"\b(never|do not|don't|does not|doesn't)\b", text, re.IGNORECASE), (
            name
        )  # what it does, only


# ---- installing -------------------------------------------------------------------------------------------------------


def test_install_puts_the_skills_in_the_runners_folder_and_uninstall_takes_them_out(box, capsys):
    pre = snapshot(box)
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0
    for name in NAMES:
        assert (box / ".claude" / "skills" / name / "SKILL.md").read_text() == skills.SKILLS[name]
    assert not (box / ".config" / "opencode").exists()
    manifest = json.loads((box / ".ws" / "install-manifest.json").read_text())
    recorded = {e["path"] for e in manifest["entries"] if e["created_by_ws"]}
    assert all(str(box / ".claude" / "skills" / n / "SKILL.md") in recorded for n in NAMES)
    after = snapshot(box)

    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "0 changes" in out and snapshot(box) == after

    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "left 0" in out
    assert snapshot(box) == pre  # ~/.claude and ~/.claude/skills were made by ws, so they go too


def test_opencode_gets_its_own_folder(box, capsys):
    assert install(capsys, box, "--runner", "opencode")[0] == 0
    for name in NAMES:
        assert (box / ".config" / "opencode" / "skills" / name / "SKILL.md").read_text() == skills.SKILLS[name]
    assert not (box / ".claude").exists()
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert not (box / ".config" / "opencode" / "skills").exists()


def test_xdg_config_home_moves_opencodes_folder(box, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(box / "xdg"))
    assert install(capsys, box, "--runner", "opencode")[0] == 0
    assert (box / "xdg" / "opencode" / "skills" / "align" / "SKILL.md").is_file()


def test_with_no_runner_chosen_ws_uses_the_ones_found_and_none_otherwise(box, capsys):
    assert install(capsys, box)[0] == 0
    assert not (box / ".claude").exists() and not (box / ".config").exists()
    (box / ".claude").mkdir()
    code, _out, _ = install(capsys, box)
    assert code == 0 and (box / ".claude" / "skills" / "align" / "SKILL.md").is_file()
    assert not (box / ".config").exists()


def test_a_skill_you_already_have_is_never_overwritten(box, capsys):
    mine = box / ".claude" / "skills" / "align"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own align\n")
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0
    assert (mine / "SKILL.md").read_text() == "my own align\n"
    assert (box / ".claude" / "skills" / "dataviz" / "SKILL.md").is_file()
    code, out, _ = run(capsys, "skills", "list")
    assert code == 0 and "yours, kept" in out and f"you already have {mine}" in out
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert (mine / "SKILL.md").read_text() == "my own align\n"  # ws never made it, so ws never removes it
    assert not (box / ".claude" / "skills" / "dataviz").exists()


def test_a_folder_of_yours_without_a_skill_file_counts_as_yours(box, capsys):
    (box / ".claude" / "skills" / "checkpoint").mkdir(parents=True)
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    assert list((box / ".claude" / "skills" / "checkpoint").iterdir()) == []


def test_opencode_does_not_get_a_copy_of_what_it_already_reads_from_claude(box, capsys):
    mine = box / ".claude" / "skills" / "dataviz"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine\n")
    assert install(capsys, box, "--runner", "opencode")[0] == 0
    assert not (box / ".config" / "opencode" / "skills" / "dataviz").exists()
    assert (box / ".config" / "opencode" / "skills" / "align" / "SKILL.md").is_file()
    _code, out, _ = run(capsys, "skills", "list", "--json")
    rows = {(r["name"], r["runner"]): r for r in json.loads(out)["data"]}
    assert rows[("dataviz", "opencode")]["action"] == "shared"
    assert rows[("align", "opencode")]["action"] == "install"
    assert install(capsys, box, "--runner", "opencode")[1].count("0 changes") == 1


def test_a_skill_you_edited_after_install_is_left_and_reported(box, capsys):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    f = box / ".claude" / "skills" / "taste-gate" / "SKILL.md"
    f.write_text(f.read_text() + "\nmy extra line\n")
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "you changed it, so ws left it" in out and str(f) in out
    assert "my extra line" in f.read_text()
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and "you changed it, so ws left it" in out
    assert f.is_file() and not (box / ".claude" / "skills" / "align").exists()


def test_a_ws_made_skill_is_updated_when_the_shipped_text_changes(box, capsys, monkeypatch):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    monkeypatch.setitem(skills.SKILLS, "align", skills.SKILLS["align"] + "\nA new line.\n")
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "1 changed" in out
    assert (box / ".claude" / "skills" / "align" / "SKILL.md").read_text().endswith("A new line.\n")


def test_switching_the_plugin_off_stops_installing_it(box, capsys):
    assert install(capsys, box)[0] == 0
    assert run(capsys, "plugin", "remove", "skills")[0] == 0
    (box / ".claude").mkdir()
    assert install(capsys, box)[0] == 0
    assert not (box / ".claude" / "skills").exists()


def test_skills_list_before_any_runner(box, capsys):
    code, out, _ = run(capsys, "skills")
    assert code == 0 and "no runner found" in out
    (box / ".claude").mkdir()
    code, out, _ = run(capsys, "skills")
    assert code == 0 and out.count("not installed yet") == len(NAMES)


def test_an_edited_manifest_cannot_point_uninstall_at_your_other_files(box, capsys):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    other = box / ".claude" / "settings.json"
    other.write_text("{}\n")
    mpath = box / ".ws" / "install-manifest.json"
    manifest = json.loads(mpath.read_text())
    manifest["entries"].append(
        {"path": str(other), "kind": "file", "sha256": hashlib.sha256(b"{}\n").hexdigest(), "created_by_ws": True}
    )
    mpath.write_text(json.dumps(manifest))
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0 and other.read_text() == "{}\n" and "outside the folders ws manages" in out


# ---- a skill you removed stays removed ----------------------------------------------------------------------------------


def remove_skill(box, name):
    d = box / ".claude" / "skills" / name
    for f in d.iterdir():
        f.unlink()
    d.rmdir()


def test_a_skill_you_deleted_is_not_recreated_and_is_reported(box, capsys):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    remove_skill(box, "dataviz")
    for _ in range(2):  # every run: left out, and said so
        code, out, _ = install(capsys, box, "--runner", "claude-code")
        assert code == 0 and "0 changes" in out
        assert "dataviz (claude-code): you removed it" in out and "ws skills add dataviz" in out
        assert not (box / ".claude" / "skills" / "dataviz").exists()
    assert "align" not in out  # the ones still there are not mentioned
    code, out, _ = run(capsys, "skills", "list")
    assert code == 0 and "removed by you" in out


def test_deleting_only_the_skill_file_counts_as_removing_it(box, capsys):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    (box / ".claude" / "skills" / "align" / "SKILL.md").unlink()
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "align (claude-code): you removed it" in out
    assert not (box / ".claude" / "skills" / "align" / "SKILL.md").exists()


def test_a_plugin_command_does_not_bring_a_removed_skill_back_either(box, capsys):
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    remove_skill(box, "checkpoint")
    assert run(capsys, "plugin", "add", "calendar")[0] == 0
    assert not (box / ".claude" / "skills" / "checkpoint").exists()


def test_skills_add_brings_a_removed_skill_back_and_uninstall_still_removes_it(box, capsys):
    pre = snapshot(box)
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    remove_skill(box, "dataviz")
    code, out, _ = run(capsys, "skills", "add", "dataviz")
    assert code == 0 and "dataviz for claude-code: 2 created" in out
    assert (box / ".claude" / "skills" / "dataviz" / "SKILL.md").read_text() == skills.SKILLS["dataviz"]
    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "0 changes" in out and "you removed it" not in out
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert snapshot(box) == pre


def test_skills_add_never_overwrites_one_you_have_and_needs_a_runner(box, capsys):
    code, _, err = run(capsys, "skills", "add", "align")
    assert code == 1 and "no runner found" in err
    mine = box / ".claude" / "skills" / "align"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine\n")
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    code, out, _ = run(capsys, "skills", "add", "align")
    assert code == 0 and "you already have" in out and (mine / "SKILL.md").read_text() == "mine\n"
    assert cli.main(["skills", "add", "nope"]) == 2


def test_skills_add_installs_one_that_was_never_installed(box, capsys):
    assert install(capsys, box, "--runner", "claude-code", "--module", "calendar")[0] == 0
    assert run(capsys, "plugin", "remove", "skills")[0] == 0
    remove_skill(box, "align")  # ws made it, so it is "removed" even with the plugin off
    assert run(capsys, "skills", "add", "align")[0] == 0
    assert (box / ".claude" / "skills" / "align" / "SKILL.md").is_file()


# ---- adopting a skill of yours that has a shipped name ----------------------------------------------------------------------


def claude_skills(box):
    return box / ".claude" / "skills"


def backups(box):
    return box / ".ws" / "backups" / "skills"


def make_mine(box, name="align", text="my own align\n"):
    mine = claude_skills(box) / name
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text(text)
    (mine / "notes.txt").write_text("extra file\n")
    return mine


def test_adopt_moves_yours_aside_installs_ws_s_and_restore_puts_it_back(box, capsys):
    mine = make_mine(box)
    inode = mine.stat().st_ino
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    assert (mine / "SKILL.md").read_text() == "my own align\n"  # yours is kept at install

    code, out, _ = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 0 and "yours is kept at" in out
    (kept,) = (backups(box) / "claude-code").iterdir()
    assert re.fullmatch(r"align-\d{8}T\d{6}Z", kept.name)
    assert (kept / "SKILL.md").read_text() == "my own align\n" and kept.stat().st_ino == inode
    assert (mine / "SKILL.md").read_text() == skills.SKILLS["align"]
    record = json.loads((backups(box) / "adopted.json").read_text())["adopted"]
    assert record["claude-code/align"]["backup"] == str(kept)

    code, out, _ = run(capsys, "skills", "list")
    assert code == 0 and f"adopted (yours kept at {kept})" in out
    rows = {(r["name"], r["runner"]): r for r in json.loads(run(capsys, "skills", "list", "--json")[1])["data"]}
    assert rows[("align", "claude-code")]["backup"] == str(kept)

    code, out, _ = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "0 changes" in out  # a second install changes nothing after the swap

    code, out, _ = run(capsys, "skills", "restore", "align", "--runner", "claude-code")
    assert code == 0 and "yours is back" in out
    assert mine.stat().st_ino == inode
    assert snapshot(mine) == {
        "SKILL.md": hashlib.sha256(b"my own align\n").hexdigest(),
        "notes.txt": hashlib.sha256(b"extra file\n").hexdigest(),
    }
    assert not kept.exists()
    assert json.loads((backups(box) / "adopted.json").read_text())["adopted"] == {}
    assert "yours, kept" in run(capsys, "skills", "list")[1]


def test_adopt_then_restore_leaves_the_skills_folder_as_it_was(box, capsys):
    make_mine(box)
    before = snapshot(claude_skills(box))
    assert run(capsys, "skills", "adopt", "align", "--runner", "claude-code")[0] == 0
    assert snapshot(claude_skills(box)) != before
    assert run(capsys, "skills", "restore", "align", "--runner", "claude-code")[0] == 0
    assert snapshot(claude_skills(box)) == before


def test_adopt_from_a_link_renames_the_link_and_restore_returns_it(box, capsys):
    real = box / "elsewhere" / "dataviz"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("linked dataviz\n")
    claude_skills(box).mkdir(parents=True)
    link = claude_skills(box) / "dataviz"
    link.symlink_to(real)
    assert run(capsys, "skills", "adopt", "dataviz", "--runner", "claude-code")[0] == 0
    (kept,) = (backups(box) / "claude-code").iterdir()
    assert kept.is_symlink() and kept.resolve() == real.resolve()
    assert (real / "SKILL.md").read_text() == "linked dataviz\n"  # the target is untouched
    assert not link.is_symlink() and (link / "SKILL.md").read_text() == skills.SKILLS["dataviz"]
    assert run(capsys, "skills", "restore", "dataviz", "--runner", "claude-code")[0] == 0
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert (real / "SKILL.md").read_text() == "linked dataviz\n"


def test_adopt_across_filesystems_is_a_typed_error_and_changes_nothing(box, capsys, monkeypatch):
    make_mine(box)
    before = snapshot(box)

    def refuse(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with monkeypatch.context() as m:
        m.setattr(os, "rename", refuse)
        code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 1 and "different filesystems" in err
    assert snapshot(box) == before and not backups(box).exists()


def test_adopt_has_nothing_to_do_without_a_skill_of_yours(box, capsys):
    (box / ".claude").mkdir()
    code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 1 and "you have no align" in err
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 1 and "already ws's own" in err
    code, _, err = run(capsys, "skills", "restore", "align", "--runner", "claude-code")
    assert code == 1 and "no adopted align" in err


def test_adopt_for_opencode_points_at_the_folder_it_reads(box, capsys):
    make_mine(box, "dataviz", "my dataviz\n")
    code, _, err = run(capsys, "skills", "adopt", "dataviz", "--runner", "opencode")
    assert code == 1 and "reads your dataviz from another folder" in err and "--runner" in err
    assert (claude_skills(box) / "dataviz" / "SKILL.md").read_text() == "my dataviz\n"


def test_a_skills_folder_that_is_a_link_still_gets_ws_s_skills(box, capsys):
    elsewhere = box / "shared-skills"
    elsewhere.mkdir()
    (box / ".claude").mkdir()
    (box / ".claude" / "skills").symlink_to(elsewhere)
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    assert sorted(p.name for p in elsewhere.iterdir()) == sorted(NAMES)  # written through the link
    rows = json.loads(run(capsys, "skills", "list", "--json")[1])["data"]
    assert {r["action"] for r in rows} == {"install"}
    assert run(capsys, "uninstall", "--yes")[0] == 0
    assert list(elsewhere.iterdir()) == [] and (box / ".claude" / "skills").is_symlink()


def test_a_skill_entry_that_is_a_link_is_listed_and_left_alone(box, capsys):
    real = box / "elsewhere" / "align"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("linked align\n")
    claude_skills(box).mkdir(parents=True)
    (claude_skills(box) / "align").symlink_to(real)
    assert install(capsys, box, "--runner", "claude-code")[0] == 0
    assert (real / "SKILL.md").read_text() == "linked align\n" and (claude_skills(box) / "align").is_symlink()
    rows = {r["name"]: r["action"] for r in json.loads(run(capsys, "skills", "list", "--json")[1])["data"]}
    assert rows["align"] == "linked" and {v for k, v in rows.items() if k != "align"} == {"install"}
    code, out, _ = run(capsys, "skills", "add", "align")
    assert code == 0 and "ws left it" in out and (real / "SKILL.md").read_text() == "linked align\n"


def test_a_failed_adopt_puts_yours_back_even_after_ws_wrote_part_of_its_own(box, capsys, monkeypatch):
    mine = make_mine(box)
    before = snapshot(box)
    real_apply = skills.install.apply

    def half_then_fail(steps, *a, **kw):
        real_apply(list(steps)[:1], *a, **kw)  # the folder is made, then the file step "fails"
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(skills.install, "apply", half_then_fail)
    code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 1 and "No space left" in err
    assert (mine / "SKILL.md").read_text() == "my own align\n" and (mine / "notes.txt").is_file()
    assert snapshot(box / ".claude") == {k[len(".claude/") :]: v for k, v in before.items() if k.startswith(".claude/")}
    assert not (backups(box) / ADOPTED).exists()
    assert json.loads(skills.install.manifest_path().read_text())["entries"] == []  # ws's half-copy is not recorded


def test_a_failed_adopt_that_cannot_put_yours_back_names_where_it_is_kept(box, capsys, monkeypatch):
    make_mine(box)
    real_apply, real_rename = skills.install.apply, os.rename

    def fail(steps, *a, **kw):
        real_apply(list(steps), *a, **kw)
        raise OSError(errno.EIO, "disk error")

    def no_rename_back(src, dst):
        if "backups" in str(src):
            raise OSError(errno.EIO, "disk error")
        real_rename(src, dst)

    monkeypatch.setattr(skills.install, "apply", fail)
    monkeypatch.setattr(os, "rename", no_rename_back)
    code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    (kept,) = (backups(box) / "claude-code").iterdir()
    assert code == 1 and str(kept) in err and "could not put your skill back" in err
    assert (kept / "SKILL.md").read_text() == "my own align\n"


def test_restore_refuses_when_the_install_record_has_no_entry_for_ws_s_copy(box, capsys):
    make_mine(box)
    assert run(capsys, "skills", "adopt", "align", "--runner", "claude-code")[0] == 0
    manifest = skills.install.manifest_path()
    doc = json.loads(manifest.read_text())
    doc["entries"] = [e for e in doc["entries"] if not e["path"].endswith("align/SKILL.md")]
    manifest.write_text(json.dumps(doc))
    code, _, err = run(capsys, "skills", "restore", "align", "--runner", "claude-code")
    assert code == 1 and "no entry for" in err and "AttributeError" not in err
    assert (claude_skills(box) / "align" / "SKILL.md").read_text() == skills.SKILLS["align"]
    assert (backups(box) / "claude-code").is_dir()


def test_a_second_adopt_is_refused_while_the_first_copy_is_unrestored(box, capsys):
    make_mine(box)
    assert run(capsys, "skills", "adopt", "align", "--runner", "claude-code")[0] == 0
    (first,) = (backups(box) / "claude-code").iterdir()
    ws_copy = claude_skills(box) / "align"
    (ws_copy / "SKILL.md").unlink()
    ws_copy.rmdir()
    skills.install.forget([ws_copy / "SKILL.md", ws_copy], os.environ)
    make_mine(box, text="my newer align\n")
    code, _, err = run(capsys, "skills", "adopt", "align", "--runner", "claude-code")
    assert code == 1 and str(first) in err and "restore align" in err
    assert (claude_skills(box) / "align" / "SKILL.md").read_text() == "my newer align\n"
    assert [p.name for p in (backups(box) / "claude-code").iterdir()] == [first.name]
    assert (first / "SKILL.md").read_text() == "my own align\n"


def test_a_corrupt_adoption_record_stops_ws_skills_but_not_ws_install(box, capsys):
    (box / ".claude").mkdir()
    backups(box).mkdir(parents=True)
    (backups(box) / ADOPTED).write_text("{not json")
    code, out, err = install(capsys, box, "--runner", "claude-code")
    assert code == 0 and "warning" in (out + err) and "adopted.json" in (out + err)
    assert sorted(p.name for p in claude_skills(box).iterdir()) == sorted(NAMES)
    for argv in (("list",), ("adopt", "align"), ("restore", "align")):
        code, _, err = run(capsys, "skills", *argv)
        assert code == 1 and "adoption record" in err, argv


def test_restore_refuses_when_ws_s_copy_has_your_edits(box, capsys):
    make_mine(box)
    assert run(capsys, "skills", "adopt", "align", "--runner", "claude-code")[0] == 0
    ours = claude_skills(box) / "align" / "SKILL.md"
    ours.write_text(ours.read_text() + "my line\n")
    code, _, err = run(capsys, "skills", "restore", "align", "--runner", "claude-code")
    assert code == 1 and "not ws's untouched copy" in err and "my line" in ours.read_text()
    assert len(list((backups(box) / "claude-code").iterdir())) == 1


def test_uninstall_after_adopt_leaves_your_copy_and_list_says_where(box, capsys):
    make_mine(box)
    assert run(capsys, "skills", "adopt", "align", "--runner", "claude-code")[0] == 0
    (kept,) = (backups(box) / "claude-code").iterdir()
    code, out, _ = run(capsys, "uninstall", "--yes")
    assert code == 0
    assert (kept / "SKILL.md").read_text() == "my own align\n"  # the backup stays
    assert not (claude_skills(box) / "align").exists()
    code, out, _ = run(capsys, "skills", "list")
    assert code == 0 and f"yours is kept at {kept}" in out
    assert run(capsys, "skills", "restore", "align", "--runner", "claude-code")[0] == 0
    assert (claude_skills(box) / "align" / "SKILL.md").read_text() == "my own align\n"


def test_adopt_and_restore_take_only_a_shipped_name_and_a_known_runner():
    assert cli.main(["skills", "adopt", "nope"]) == 2
    assert cli.main(["skills", "restore", "align", "--runner", "nope"]) == 2


def test_an_adoption_record_that_does_not_read_stops_the_command(box, capsys):
    backups(box).mkdir(parents=True)
    (backups(box) / "adopted.json").write_text("{not json")
    (box / ".claude").mkdir()
    code, _, err = run(capsys, "skills", "list")
    assert code == 1 and "does not read as the adoption record" in err
