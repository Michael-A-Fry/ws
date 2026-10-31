"""The module registry and `ws module list|add|remove`: idempotent, and edits [modules] through install.plan/apply."""

from __future__ import annotations

import json
import tomllib

import pytest

from ws import cli, modules


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def profile(box) -> dict:
    return tomllib.loads((box / ".ws" / "profile.toml").read_text())


def test_registry_defaults():
    assert modules.default_on() == {"memory", "notes"}
    assert set(modules.REGISTRY) == {"memory", "notes", "mail-drafts", "calendar", "finance", "knowledge-graph"}
    for name in ("mail-drafts", "calendar", "finance", "knowledge-graph"):
        m = modules.REGISTRY[name]
        assert not m.default_on and not m.built and m.status == "not built yet" and m.what and m.table


def test_list_before_install_shows_the_defaults(box, capsys):
    code, out, _ = run(capsys, "module", "list", "--json")
    rows = {r["name"]: r for r in json.loads(out)}
    assert code == 0
    assert rows["memory"]["on"] and rows["notes"]["on"] and not rows["finance"]["on"]
    code, out, _ = run(capsys, "module", "list")
    assert "finance" in out and "not built yet" in out


def test_add_and_remove_need_a_profile_and_a_known_name(box, capsys):
    code, _, err = run(capsys, "module", "add", "finance")
    assert code == 1 and "ws install" in err and not (box / ".ws").exists()
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, _, err = run(capsys, "module", "add", "nope")
    assert code == 1 and "unknown module" in err


def test_adding_a_stub_records_the_choice_and_is_idempotent(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, out, _ = run(capsys, "module", "add", "finance")
    assert code == 0 and "1 changed" in out and "not built yet" in out
    assert profile(box)["modules"]["finance"] is True
    code, out, _ = run(capsys, "module", "add", "finance")
    assert code == 0 and "0 changes" in out
    code, out, _ = run(capsys, "module", "list", "--json")
    assert {r["name"]: r["on"] for r in json.loads(out)}["finance"] is True


def test_removing_notes_drops_its_role_and_keeps_the_folder(box, capsys):
    notes = box / "Notes"
    assert run(capsys, "install", "--yes", "--notes", str(notes))[0] == 0
    (notes / "_inbox" / "n.md").write_text("mine")
    code, out, _ = run(capsys, "module", "remove", "notes")
    assert code == 0 and "off" in out
    prof = profile(box)
    assert prof["modules"]["notes"] is False and "notes" not in prof["roles"]
    assert (notes / "_inbox" / "n.md").read_text() == "mine"
    assert "0 changes" in run(capsys, "module", "remove", "notes")[1]
    assert run(capsys, "module", "add", "notes")[0] == 0
    assert profile(box)["roles"]["notes"]["path"] == str(notes)


def test_add_keeps_the_runner_and_cap_chosen_at_install(box, capsys):
    assert (
        run(capsys, "install", "--yes", "--runner", "claude-code", "--cap", "12", "--notes", str(box / "Notes"))[0] == 0
    )
    assert run(capsys, "module", "add", "calendar")[0] == 0
    prof = profile(box)
    assert prof["roles"]["runner"]["adapter"] == "claude-code" and prof["roles"]["budget"]["monthly_cap_usd"] == 12
    assert "0 changes" in run(capsys, "install", "--yes")[1]


def test_a_module_with_no_modules_table_counts_as_the_defaults(box, capsys):
    p = box / ".ws" / "profile.toml"
    p.parent.mkdir()
    p.write_text('[machine]\nnotes = "' + str(box / "Notes") + '"\n')
    (box / "Notes").mkdir()
    code, _, _ = run(capsys, "module", "remove", "memory")
    assert code == 0
    assert profile(box)["modules"]["memory"] is False
