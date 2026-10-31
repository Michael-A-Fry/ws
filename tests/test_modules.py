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
    monkeypatch.setattr(modules, "guess_region", lambda env: "NZ")  # the machine running the tests is not the test
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def profile(box) -> dict:
    return tomllib.loads((box / ".ws" / "profile.toml").read_text())


def test_registry_defaults():
    assert modules.default_on() == {"memory", "notes", "skills"}
    assert set(modules.REGISTRY) == {
        "memory",
        "notes",
        "skills",
        "mail-drafts",
        "calendar",
        "finance",
        "knowledge-graph",
    }
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
    assert code == 1 and "unknown plugin" in err


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


# --- `ws plugin`, and regions -----------------------------------------------------------------------------------------


def test_plugin_is_the_command_and_module_still_works_but_is_hidden(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, out, _ = run(capsys, "plugin", "list", "--json")
    assert code == 0 and {r["name"] for r in json.loads(out)} == set(modules.REGISTRY)
    assert run(capsys, "plugin", "add", "calendar")[0] == 0 and profile(box)["modules"]["calendar"] is True
    assert run(capsys, "module", "remove", "calendar")[0] == 0 and profile(box)["modules"]["calendar"] is False
    help_text = cli.build_parser().format_help()
    assert help_text.count("switch parts of ws on or off") == 1


def test_only_finance_has_a_region():
    assert {n: m.region for n, m in modules.REGISTRY.items() if m.region} == {"finance": ("NZ",)}


def test_finance_refuses_outside_nz_with_one_plain_line(box, capsys, monkeypatch):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    monkeypatch.setattr(modules, "guess_region", lambda env: "US")
    before = (box / ".ws" / "profile.toml").read_text()
    code, out, err = run(capsys, "plugin", "add", "finance")
    assert code == 1 and not out
    assert err.count("\n") == 1 and "finance is for NZ only" in err and "US" in err and "[machine]" in err
    assert (box / ".ws" / "profile.toml").read_text() == before
    assert run(capsys, "plugin", "add", "calendar")[0] == 0  # a plugin with no region is not affected
    assert run(capsys, "plugin", "remove", "finance")[0] == 0  # switching off is always allowed


def test_the_profile_region_beats_the_guess(box, capsys, monkeypatch):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    p = box / ".ws" / "profile.toml"
    monkeypatch.setattr(modules, "guess_region", lambda env: "US")
    p.write_text(p.read_text().replace("\n[machine]\n", '\n[machine]\nregion = "nz"\n', 1))
    assert run(capsys, "plugin", "add", "finance")[0] == 0
    p.write_text(p.read_text().replace('region = "nz"', 'region = "AU"'))
    monkeypatch.setattr(modules, "guess_region", lambda env: "NZ")
    code, _, err = run(capsys, "plugin", "remove", "finance")
    assert code == 0
    code, _, err = run(capsys, "plugin", "add", "finance")
    assert code == 1 and "your profile says AU" in err


def test_an_unknown_region_does_not_refuse(box, capsys, monkeypatch):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    monkeypatch.setattr(modules, "guess_region", lambda env: None)
    assert run(capsys, "plugin", "add", "finance")[0] == 0


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"TZ": "Pacific/Auckland"}, "NZ"),
        ({"TZ": ":Pacific/Chatham"}, "NZ"),
        ({"TZ": "UTC", "LANG": "en_NZ.UTF-8"}, "NZ"),
        ({"TZ": "UTC", "LC_ALL": "en_AU.UTF-8", "LANG": "en_NZ.UTF-8"}, "AU"),
        ({"TZ": "UTC", "LANG": "C.UTF-8"}, None),
        ({"TZ": "UTC"}, None),
    ],
)
def test_the_region_guess(env, want, tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "ZONE_TAB", tmp_path / "no-zone.tab")
    assert modules.guess_region(env) == want


def test_the_zone_table_gives_other_countries(tmp_path, monkeypatch):
    tab = tmp_path / "zone.tab"
    tab.write_text("# comment\nUS\t+404251-0740023\tAmerica/New_York\tEastern\nNZ\t-3652+17446\tPacific/Auckland\n")
    monkeypatch.setattr(modules, "ZONE_TAB", tab)
    assert modules.guess_region({"TZ": "America/New_York"}) == "US"
