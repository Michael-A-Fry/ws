"""The module registry and `ws module list|add|remove`: idempotent, and edits [modules] through install.plan/apply."""

from __future__ import annotations

import json
import os
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
    monkeypatch.delenv(modules.SHOW_UNBUILT, raising=False)  # the default view: built plugins only
    return home


@pytest.fixture
def unbuilt(box, monkeypatch):
    """The developer view: plugins still being built are listed and can be switched on."""
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    return box


def in_region(monkeypatch, country):
    """Put this test's machine in a country (None = nowhere in particular) whatever zone and locale the host has."""
    monkeypatch.setattr(modules, "zone_region", lambda env: country)
    monkeypatch.setattr(modules, "locale_region", lambda env: None)


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


def test_only_built_plugins_are_offered(box, monkeypatch):
    assert set(modules.offered()) == {"memory", "notes", "skills"}
    assert all(m.built for m in modules.offered().values())
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    assert set(modules.offered()) == set(modules.REGISTRY)
    monkeypatch.setenv(modules.SHOW_UNBUILT, "0")
    assert set(modules.offered()) == {"memory", "notes", "skills"}


def test_list_before_install_shows_the_defaults(box, capsys):
    code, out, _ = run(capsys, "module", "list", "--json")
    rows = {r["name"]: r for r in json.loads(out)["data"]}
    assert code == 0
    assert set(rows) == {"memory", "notes", "skills"} and all(r["on"] for r in rows.values())
    code, out, _ = run(capsys, "module", "list")
    assert code == 0 and "not built yet" not in out


@pytest.mark.parametrize("command", ["plugin", "module"])
def test_list_never_names_a_plugin_that_is_not_built(box, capsys, command):
    unbuilt_names = {n for n, m in modules.REGISTRY.items() if not m.built}
    assert unbuilt_names
    for argv in ((command, "list"), (command, "list", "--json")):
        code, out, err = run(capsys, *argv)
        assert code == 0
        for name in unbuilt_names:
            assert name not in out and name not in err


def test_the_developer_view_lists_every_plugin_and_says_which_are_not_built(unbuilt, capsys):
    code, out, _ = run(capsys, "plugin", "list")
    assert code == 0 and "finance" in out and "not built yet" in out
    rows = {r["name"]: r for r in json.loads(run(capsys, "plugin", "list", "--json")[1])["data"]}
    assert set(rows) == set(modules.REGISTRY) and rows["finance"]["status"] == "not built yet"


def test_help_does_not_name_an_unbuilt_plugin(box, capsys):
    help_text = cli.build_parser().format_help()
    for name in (n for n, m in modules.REGISTRY.items() if not m.built):
        assert name not in help_text


def test_add_and_remove_need_a_profile_and_a_known_name(box, capsys):
    code, _, err = run(capsys, "module", "add", "skills")
    assert code == 1 and "ws install" in err and not (box / ".ws").exists()
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, _, err = run(capsys, "module", "add", "nope")
    assert code == 1 and "unknown plugin" in err


def test_an_unbuilt_plugin_cannot_be_switched_on_unless_a_developer_asks(box, capsys):
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    before = (box / ".ws" / "profile.toml").read_text()
    code, _, err = run(capsys, "plugin", "add", "calendar")
    assert code == 1 and "unknown plugin" in err and "known: memory, notes, skills" in err
    assert (box / ".ws" / "profile.toml").read_text() == before


def test_install_does_not_take_an_unbuilt_plugin_by_name_either(box, capsys, monkeypatch):
    code, _, err = run(capsys, "install", "--yes", "--notes", str(box / "Notes"), "--module", "finance")
    assert code == 1 and "unknown module 'finance'" in err and "known: memory, notes, skills" in err
    assert not (box / ".ws" / "profile.toml").exists()
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"), "--module", "finance")[0] == 0


def test_adding_a_stub_records_the_choice_and_is_idempotent(unbuilt, capsys):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, out, _ = run(capsys, "module", "add", "knowledge-graph")
    assert code == 0 and "1 changed" in out and "not built yet" in out
    assert profile(box)["modules"]["knowledge-graph"] is True
    code, out, _ = run(capsys, "module", "add", "knowledge-graph")
    assert code == 0 and "0 changes" in out
    code, out, _ = run(capsys, "module", "list", "--json")
    assert {r["name"]: r["on"] for r in json.loads(out)["data"]}["knowledge-graph"] is True


def test_a_profile_with_an_unbuilt_plugin_on_still_works_and_it_can_be_switched_off(box, capsys, monkeypatch):
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    assert run(capsys, "plugin", "add", "calendar")[0] == 0
    monkeypatch.delenv(modules.SHOW_UNBUILT)
    code, out, _ = run(capsys, "plugin", "list")
    assert code == 0 and "calendar" not in out
    assert run(capsys, "install", "--yes")[0] == 0 and profile(box)["modules"]["calendar"] is True  # kept, not dropped
    assert run(capsys, "plugin", "remove", "calendar")[0] == 0 and profile(box)["modules"]["calendar"] is False


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


def test_add_keeps_the_runner_and_cap_chosen_at_install(unbuilt, capsys):
    box = unbuilt
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
    assert code == 0 and {r["name"] for r in json.loads(out)["data"]} == set(modules.offered())
    assert run(capsys, "plugin", "add", "skills")[0] == 0 and profile(box)["modules"]["skills"] is True
    assert run(capsys, "module", "remove", "skills")[0] == 0 and profile(box)["modules"]["skills"] is False
    help_text = cli.build_parser().format_help()
    assert help_text.count("switch parts of ws on or off") == 1


def test_only_finance_has_a_region():
    assert {n: m.region for n, m in modules.REGISTRY.items() if m.region} == {"finance": ("NZ",)}


def test_finance_refuses_outside_nz_with_one_plain_line(unbuilt, capsys, monkeypatch):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    in_region(monkeypatch, "US")
    before = (box / ".ws" / "profile.toml").read_text()
    code, out, err = run(capsys, "plugin", "add", "finance")
    assert code == 1 and not out
    assert err.count("\n") == 1 and "finance is for NZ only" in err and "time zone is in US" in err
    assert "ws plugin add finance --region NZ" in err
    assert (box / ".ws" / "profile.toml").read_text() == before
    assert run(capsys, "plugin", "add", "calendar")[0] == 0  # a plugin with no region is not affected
    assert run(capsys, "plugin", "remove", "finance")[0] == 0  # switching off is always allowed


def test_the_profile_region_beats_the_time_zone(unbuilt, capsys, monkeypatch):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    p = box / ".ws" / "profile.toml"
    in_region(monkeypatch, "US")
    p.write_text(p.read_text().replace("\n[machine]\n", '\n[machine]\nregion = "nz"\n', 1))
    assert run(capsys, "plugin", "add", "finance")[0] == 0
    p.write_text(p.read_text().replace('region = "nz"', 'region = "AU"'))
    in_region(monkeypatch, "NZ")
    assert run(capsys, "plugin", "remove", "finance")[0] == 0
    code, _, err = run(capsys, "plugin", "add", "finance")
    assert code == 1 and "your profile says AU" in err


def test_region_flag_writes_the_profile_and_lets_finance_on(unbuilt, capsys, monkeypatch):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    in_region(monkeypatch, "US")
    assert run(capsys, "plugin", "add", "finance")[0] == 1
    code, out, _ = run(capsys, "plugin", "add", "finance", "--region", "nz")
    assert code == 0 and "region set to NZ" in out
    assert profile(box)["machine"]["region"] == "NZ" and profile(box)["modules"]["finance"] is True
    assert profile(box)["machine"]["notes"] == str(box / "Notes")  # the rest of [machine] is kept
    assert run(capsys, "plugin", "add", "finance")[0] == 0  # remembered: no flag needed now


def test_region_flag_that_does_not_fit_is_refused_and_writes_nothing(unbuilt, capsys, monkeypatch):
    box = unbuilt
    in_region(monkeypatch, None)
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    before = (box / ".ws" / "profile.toml").read_text()
    code, _, err = run(capsys, "plugin", "add", "finance", "--region", "AU")
    assert code == 1 and "you said AU" in err
    code, _, err = run(capsys, "plugin", "add", "finance", "--region", "Aotearoa")
    assert code == 1 and "two-letter" in err
    assert (box / ".ws" / "profile.toml").read_text() == before


def test_an_unknown_region_does_not_refuse(unbuilt, capsys, monkeypatch):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    in_region(monkeypatch, None)
    assert run(capsys, "plugin", "add", "finance")[0] == 0


def test_a_utc_server_with_a_us_locale_is_warned_not_refused(unbuilt, capsys, monkeypatch):
    box = unbuilt
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setattr(modules, "ZONE_TAB", box / "no-zone.tab")
    code, out, err = run(capsys, "plugin", "add", "finance")
    assert code == 0 and "finance is on" in out
    assert "your locale says US" in err and "only a guess" in err and "--region NZ" in err


def test_the_region_comes_from_the_profile_then_the_zone_then_the_locale(box, monkeypatch):
    monkeypatch.setattr(modules, "ZONE_TAB", box / "no-zone.tab")
    env = {"TZ": "Pacific/Auckland", "LANG": "en_US.UTF-8", "WS_HOME": str(box / ".ws"), "HOME": str(box)}
    assert modules.region_of(env) == ("NZ", "zone")
    assert modules.region_of({**env, "TZ": "UTC"}) == ("US", "locale")
    assert modules.region_of({**env, "TZ": "UTC", "LANG": "C"}) == (None, None)
    (box / ".ws").mkdir(exist_ok=True)
    (box / ".ws" / "profile.toml").write_text('[machine]\nregion = "au"\n')
    assert modules.region_of(env) == ("AU", "profile")


@pytest.mark.parametrize(
    ("tz", "want"),
    [
        ("Pacific/Auckland", "NZ"),
        (":Pacific/Chatham", "NZ"),
        ("NZ", "NZ"),
        ("NZST-12NZDT", "NZ"),
        ("NZST-12NZDT,M9.5.0,M4.1.0/3", "NZ"),
        ("UTC", None),
        ("Etc/UTC", None),
        ("EST5EDT", None),
    ],
)
def test_the_zone_region(tz, want, tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "ZONE_TAB", tmp_path / "no-zone.tab")
    assert modules.zone_region({"TZ": tz}) == want


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"LANG": "en_NZ.UTF-8"}, "NZ"),
        ({"LC_ALL": "en_AU.UTF-8", "LANG": "en_NZ.UTF-8"}, "AU"),
        ({"LANG": "C.UTF-8"}, None),
        ({}, None),
    ],
)
def test_the_locale_region(env, want):
    assert modules.locale_region(env) == want


def test_the_zone_table_gives_other_countries(tmp_path, monkeypatch):
    tab = tmp_path / "zone.tab"
    tab.write_text("# comment\nUS\t+404251-0740023\tAmerica/New_York\tEastern\nNZ\t-3652+17446\tPacific/Auckland\n")
    monkeypatch.setattr(modules, "ZONE_TAB", tab)
    assert modules.zone_region({"TZ": "America/New_York"}) == "US"


ZONE_TAB_TEXT = (
    "# comment\nUS\t+340308-1181434\tAmerica/Los_Angeles\tPacific\nNZ\t-3652+17446\tPacific/Auckland\tNew Zealand\n"
)


@pytest.mark.parametrize(
    ("tz", "want"),
    [("UTC", None), ("Pacific/Auckland", "NZ"), ("America/Los_Angeles", "US")],
)
def test_finance_follows_the_zone_the_test_sets_whatever_the_host_zone_is(unbuilt, capsys, monkeypatch, tz, want):
    """The whole path, from $TZ (set here; ws reads the variable, not the C library's zone) to the plugin command."""
    box = unbuilt
    tab = box / "zone.tab"
    tab.write_text(ZONE_TAB_TEXT)
    monkeypatch.setattr(modules, "ZONE_TAB", tab)
    monkeypatch.setattr(modules, "locale_region", lambda env: None)
    monkeypatch.setenv("TZ", tz)
    assert modules.zone_region(os.environ) == want
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, _, err = run(capsys, "plugin", "add", "finance")
    if want == "US":
        assert code == 1 and "finance is for NZ only" in err and "time zone is in US" in err
    else:
        assert code == 0
