"""ws globals: a planted shadow fails, a listed override passes and is reported, a stale or reasonless one is caught,
and a manifest stands in for the user config (CI has none)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import cli, doctor
from ws import globals as wsg
from ws.adapters import claude_code, opencode

SKILL = "---\nname: align\n---\nAsk why before what, and ask it of the person who owns the answer.\n"
AGENT = "You review diffs with a fresh context and report only what you can point to in the diff.\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake home with a global skill and agent, and an empty git repo."""
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "align").mkdir(parents=True)
    (home / ".claude" / "skills" / "align" / "SKILL.md").write_text(SKILL)
    (home / ".claude" / "agents").mkdir()
    (home / ".claude" / "agents" / "reviewer.md").write_text(AGENT)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(wsg.MANIFEST_ENV, raising=False)
    return home, repo


def put(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def run(capsys, repo, *extra) -> tuple[int, str]:
    code = cli.main(["globals", "check", "--repo", str(repo), *extra])
    return code, capsys.readouterr().out


def test_a_repo_with_no_copies_passes(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/own-thing/SKILL.md", "Only this repo does this.\n")
    code, out = run(capsys, repo)
    assert code == 0
    assert "0 shadows" in out


def test_same_name_is_a_shadow(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "My own take on align.\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "shadow: .claude/skills/align — same name as global skills/align" in out


def test_same_agent_file_name_is_a_shadow(world, capsys):
    _, repo = world
    put(repo, ".claude/agents/reviewer.md", "different words\n")
    assert run(capsys, repo)[0] == 1


def test_a_renamed_byte_identical_copy_is_a_shadow(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/my-align/SKILL.md", SKILL)
    code, out = run(capsys, repo)
    assert code == 1
    assert "shadow: .claude/skills/my-align/SKILL.md — same content as global skills/align/SKILL.md" in out


def test_a_workflow_matches_on_its_stem(world, capsys):
    home, repo = world
    put(home, ".claude/workflows/build.js", "export default 1\n")
    put(repo, ".claude/workflows/build.ts", "export default 2\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "same name as global workflows/build.js" in out


def test_a_listed_override_passes_and_is_reported(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "My own take on align.\n")
    put(
        repo,
        ".ws/overrides.toml",
        '[[override]]\npath = ".claude/skills/align"\nreason = "this repo asks different questions"\n',
    )
    code, out = run(capsys, repo)
    assert code == 0
    assert "override: .claude/skills/align — this repo asks different questions (promote to global or drop)" in out


def test_an_override_can_name_one_file(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/my-align/SKILL.md", SKILL)
    put(repo, ".ws/overrides.toml", '[[override]]\npath = ".claude/skills/my-align/SKILL.md"\nreason = "pinned copy"\n')
    assert run(capsys, repo)[0] == 0


@pytest.mark.parametrize("reason", ['""', '"   "'])
def test_an_override_with_no_reason_fails(world, capsys, reason):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    put(repo, ".ws/overrides.toml", f'[[override]]\npath = ".claude/skills/align"\nreason = {reason}\n')
    code, out = run(capsys, repo)
    assert code == 1
    assert "a reason is required" in out


def test_an_override_missing_the_reason_key_fails(world, capsys):
    _, repo = world
    put(repo, ".ws/overrides.toml", '[[override]]\npath = ".claude/skills/align"\n')
    assert run(capsys, repo)[0] == 1


def test_an_override_for_nothing_is_stale_and_reported(world, capsys):
    _, repo = world
    put(repo, ".ws/overrides.toml", '[[override]]\npath = ".claude/skills/gone"\nreason = "was needed once"\n')
    code, out = run(capsys, repo)
    assert code == 0
    assert "stale override: .claude/skills/gone" in out


def test_a_broken_overrides_file_fails(world, capsys):
    _, repo = world
    put(repo, ".ws/overrides.toml", "[[override\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "does not parse" in out


def test_a_link_to_the_global_is_not_a_shadow(world, capsys):
    home, repo = world
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "align").symlink_to(home / ".claude" / "skills" / "align")
    assert run(capsys, repo)[0] == 0


def test_empty_files_never_match_on_content(world, capsys):
    home, repo = world
    put(home, ".claude/hooks/empty.sh", "")
    put(repo, ".claude/hooks/other.sh", "")
    assert run(capsys, repo)[0] == 0


def test_json_report(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    code, out = run(capsys, repo, "--json")
    env = json.loads(out)
    rep = env["data"]
    assert code == 1 and env["status"] == "attention"
    assert rep["status"] == "fail"
    assert rep["shadows"][0]["path"] == ".claude/skills/align"


# --- manifest ------------------------------------------------------------------------------------------------------


def make_manifest(world, capsys, tmp_path) -> str:
    assert cli.main(["globals", "manifest"]) == 0
    text = capsys.readouterr().out
    assert "Ask why before what" not in text and "reviewer" in text  # names and hashes, never content
    f = tmp_path / "globals.json"
    f.write_text(text)
    return str(f)


def test_manifest_mode_works_with_no_user_config(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    manifest = make_manifest(world, capsys, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))  # CI: no ~/.claude
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    code, out = run(capsys, repo, "--manifest", manifest)
    assert code == 1
    assert "same name as global skills/align" in out
    assert "from manifest" in out


def test_manifest_from_the_environment_catches_a_renamed_copy(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    manifest = make_manifest(world, capsys, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    monkeypatch.setenv(wsg.MANIFEST_ENV, manifest)
    put(repo, ".claude/agents/copy-of-reviewer.md", AGENT)
    assert run(capsys, repo)[0] == 1


def test_no_user_config_and_no_manifest_is_a_warning_not_a_pass(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    code, out = run(capsys, repo)
    assert code == 0
    assert "warn: nothing to compare" in out
    assert run(capsys, repo, "--strict")[0] == 1


def test_a_bad_manifest_fails(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    code, out = run(capsys, repo, "--manifest", str(bad))
    assert code == 1
    assert "not a ws globals manifest" in out


def test_manifest_with_no_globals_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["globals", "manifest"]) == 1


# --- doctor WS005 --------------------------------------------------------------------------------------------------


def ws005(tmp_path, monkeypatch, repo) -> dict:
    monkeypatch.chdir(repo)
    from ws import osinfo

    osi = osinfo.OSInfo("linux", "x", "x86_64", "systemd", True)
    checks = doctor.run_checks(osi, tmp_path / "none.toml", network=False)
    return {c.id: c for c in checks}["WS005"]


def test_doctor_fails_on_a_shadow_and_passes_when_listed(world, tmp_path, monkeypatch):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    c = ws005(tmp_path, monkeypatch, repo)
    assert (c.status, c.role) == ("fail", "globals")
    assert ".claude/skills/align" in c.detail
    put(repo, ".ws/overrides.toml", '[[override]]\npath = ".claude/skills/align"\nreason = "different"\n')
    c = ws005(tmp_path, monkeypatch, repo)
    assert c.status == "ok"
    assert "1 listed override" in c.detail


def test_doctor_outside_a_repo_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plain = tmp_path / "plain"
    plain.mkdir()
    c = ws005(tmp_path, monkeypatch, plain)
    assert (c.status, c.detail) == ("ok", "not in a repo")


def test_doctor_warns_when_there_is_nothing_to_compare(world, tmp_path, monkeypatch):
    _, repo = world
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    assert ws005(tmp_path, monkeypatch, repo).status == "warn"


# --- the runner layouts --------------------------------------------------------------------------------------------


def test_opencode_layout_is_checked_too(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    put(home, ".config/opencode/agents/helper.md", "an opencode agent\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    put(repo, ".opencode/agents/helper.md", "changed\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    code, out = run(capsys, repo)
    assert code == 1
    assert "shadow: .opencode/agents/helper.md" in out


def test_every_layout_has_the_keys_the_check_reads():
    for layout in (claude_code.GLOBALS, opencode.GLOBALS):
        assert {"user_dir", "project_dir", "kinds"} <= set(layout)
        assert set(layout["kinds"].values()) <= {"dir", "name", "stem"}


def test_this_repo_has_no_shadows(capsys):

    root = Path(__file__).resolve().parent.parent
    assert run(capsys, root)[0] == 0


# --- links, small files, folders, overrides, nesting ---------------------------------------------------------------


def test_a_link_through_a_linked_global_folder_is_not_a_shadow(tmp_path, monkeypatch, capsys):
    """The real layout: ~/.claude/skills is itself a link to a folder kept elsewhere."""
    home = tmp_path / "home"
    kept = home / "kept" / "skills"
    put(kept, "align/SKILL.md", SKILL)
    (home / ".claude").mkdir()
    (home / ".claude" / "skills").symlink_to(kept)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "align").symlink_to(home / ".claude" / "skills" / "align")  # via the global link
    (repo / ".claude" / "skills" / "align2").symlink_to(kept / "align")  # straight to where it is kept
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(wsg.MANIFEST_ENV, raising=False)
    code, out = run(capsys, repo)
    assert code == 0, out
    put(repo, ".claude/skills/align3/SKILL.md", SKILL)  # a real copy of it is still caught
    assert run(capsys, repo)[0] == 1


def test_small_shared_files_are_not_copies(world, capsys):
    home, repo = world
    put(home, ".claude/skills/align/requirements.txt", "pyyaml\n")
    put(home, ".claude/hooks/ok.sh", "exit 0\n")
    put(repo, ".claude/skills/other/SKILL.md", "Something this repo only does here, written out in full.\n")
    put(repo, ".claude/skills/other/requirements.txt", "pyyaml\n")
    put(repo, ".claude/hooks/fine.sh", "exit 0\n")
    code, out = run(capsys, repo)
    assert code == 0, out


def test_a_renamed_whole_folder_is_caught_by_its_digest(world, capsys):
    home, repo = world
    put(home, ".claude/skills/tiny/SKILL.md", "x\n")
    put(home, ".claude/skills/tiny/notes.md", "y" * 80 + "\n")
    put(repo, ".claude/skills/renamed/SKILL.md", "x\n")
    put(repo, ".claude/skills/renamed/notes.md", "y" * 80 + "\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "shadow: .claude/skills/renamed — same content as global skills/tiny" in out


def test_names_match_whatever_the_case(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/ALIGN/SKILL.md", "mine\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "same name as global skills/align" in out


def test_commands_are_a_kind(world, capsys):
    home, repo = world
    put(home, ".claude/commands/ship.md", "ship it properly\n")
    put(repo, ".claude/commands/ship.md", "ship it my way\n")
    assert run(capsys, repo)[0] == 1


def test_opencode_skills_are_a_kind(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    put(home, ".config/opencode/skills/align/SKILL.md", SKILL)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    put(repo, ".opencode/skills/align/SKILL.md", "mine\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert run(capsys, repo)[0] == 1


def test_nested_config_folders_are_scanned_but_not_vendored_or_inner_repos(world, capsys):
    _, repo = world
    put(repo, "node_modules/pkg/.claude/skills/align/SKILL.md", "vendored\n")
    put(repo, ".venv/lib/.claude/skills/align/SKILL.md", "vendored\n")
    put(repo, "other/.claude/skills/align/SKILL.md", "an inner repo\n")
    (repo / "other" / ".git").mkdir()
    put(repo, ".claude/worktrees/w1/.claude/skills/align/SKILL.md", "a worktree\n")
    assert run(capsys, repo)[0] == 0
    put(repo, "services/api/.claude/skills/align/SKILL.md", "mine\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "shadow: services/api/.claude/skills/align" in out


def test_an_override_can_cover_a_nested_item(world, capsys):
    _, repo = world
    put(repo, "services/api/.claude/skills/align/SKILL.md", "mine\n")
    put(
        repo,
        ".ws/overrides.toml",
        '[[override]]\npath = "services/api/.claude/skills/align"\nreason = "the api team asks differently"\n',
    )
    assert run(capsys, repo)[0] == 0


@pytest.mark.parametrize("path", [".claude", ".claude/skills", ".", "services", ".claude/skills/../.."])
def test_an_override_above_an_item_is_rejected(world, capsys, path):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    put(repo, ".ws/overrides.toml", f'[[override]]\npath = "{path}"\nreason = "everything"\n')
    code, out = run(capsys, repo)
    assert code == 1
    assert "too broad" in out


def test_a_duplicate_override_is_reported(world, capsys):
    _, repo = world
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    row = '[[override]]\npath = ".claude/skills/align"\nreason = "r"\n'
    put(repo, ".ws/overrides.toml", row + row)
    code, out = run(capsys, repo)
    assert code == 1
    assert "listed twice" in out


def test_a_manifest_committed_in_the_repo_is_used(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    manifest = make_manifest(world, capsys, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    put(repo, ".ws/globals.json", Path(manifest).read_text())
    put(repo, ".claude/skills/align/SKILL.md", "mine\n")
    code, out = run(capsys, repo)
    assert code == 1
    assert "from manifest" in out


def test_the_nothing_to_compare_line_says_so_plainly(world, capsys, tmp_path, monkeypatch):
    _, repo = world
    monkeypatch.setenv("HOME", str(tmp_path / "ci-home"))
    assert "this run checked nothing" in run(capsys, repo)[1]


def test_the_ci_hook_prints_its_output():

    cfg = (Path(__file__).resolve().parent.parent / ".pre-commit-config.yaml").read_text()
    block = cfg[cfg.index("id: ws-globals") :]
    assert "verbose: true" in block
