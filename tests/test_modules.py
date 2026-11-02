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


@pytest.fixture
def regional(box, monkeypatch):
    """Built plugins with a region rule of their own, so the rule is tested whatever the real registry holds."""
    for name, regions in (("ledger", ("AU", "GB")), ("tax-pack", ("CA",))):
        plugin = modules.Module(name, f"{name} for some countries", False, f"roles.{name}", True, region=regions)
        monkeypatch.setitem(modules.REGISTRY, name, plugin)
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


def test_no_built_in_plugin_is_tied_to_a_country():
    assert [n for n, m in modules.REGISTRY.items() if m.region] == []
    assert modules.needs_region({}) is False


def test_a_plugin_s_own_metadata_makes_the_region_matter(regional):
    assert modules.needs_region({}) is True
    assert {n: m.region for n, m in modules.offered({}).items() if m.region} == {
        "ledger": ("AU", "GB"),
        "tax-pack": ("CA",),
    }


def test_the_region_table_is_consistent():
    assert all(code == r.code and len(code) == 2 and code.isupper() for code, r in modules.REGIONS.items())
    assert len(modules._BY_ZONE) == sum(len(r.zones) for r in modules.REGIONS.values())  # no zone in two countries
    assert len(modules._BY_ABBREVIATION) == sum(len(r.abbreviations) for r in modules.REGIONS.values())


@pytest.mark.parametrize(
    ("here", "ok"),
    [("AU", True), ("GB", True), ("US", False), ("NZ", False), ("CA", False)],
)
def test_a_plugin_refuses_outside_its_regions_with_one_plain_line(regional, capsys, monkeypatch, here, ok):
    assert run(capsys, "install", "--yes", "--notes", str(regional / "Notes"))[0] == 0
    in_region(monkeypatch, here)
    before = (regional / ".ws" / "profile.toml").read_text()
    code, out, err = run(capsys, "plugin", "add", "ledger")
    if ok:
        assert code == 0 and "ledger is on" in out
        return
    assert code == 1 and not out
    assert err.count("\n") == 1 and "ledger is for AU/GB only" in err and f"time zone is in {here}" in err
    assert "ws plugin add ledger --region AU" in err
    assert (regional / ".ws" / "profile.toml").read_text() == before
    assert run(capsys, "plugin", "add", "calendar")[0] == 1  # unbuilt and hidden, so not offered at all
    assert run(capsys, "plugin", "add", "skills")[0] == 0  # a plugin with no region is not affected
    assert run(capsys, "plugin", "remove", "ledger")[0] == 0  # switching off is always allowed


def test_each_plugin_uses_its_own_region_rule(regional, capsys, monkeypatch):
    assert run(capsys, "install", "--yes", "--notes", str(regional / "Notes"))[0] == 0
    in_region(monkeypatch, "CA")
    assert run(capsys, "plugin", "add", "tax-pack")[0] == 0
    code, _, err = run(capsys, "plugin", "add", "ledger")
    assert code == 1 and "ledger is for AU/GB only" in err and "time zone is in CA" in err


def test_the_profile_region_beats_the_time_zone(regional, capsys, monkeypatch):
    box = regional
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    p = box / ".ws" / "profile.toml"
    in_region(monkeypatch, "US")
    p.write_text(p.read_text().replace("\n[machine]\n", '\n[machine]\nregion = "gb"\n', 1))
    assert run(capsys, "plugin", "add", "ledger")[0] == 0
    p.write_text(p.read_text().replace('region = "gb"', 'region = "DE"'))
    in_region(monkeypatch, "AU")
    assert run(capsys, "plugin", "remove", "ledger")[0] == 0
    code, _, err = run(capsys, "plugin", "add", "ledger")
    assert code == 1 and "your profile says DE" in err


def test_region_flag_writes_the_profile_and_lets_the_plugin_on(regional, capsys, monkeypatch):
    box = regional
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    in_region(monkeypatch, "US")
    assert run(capsys, "plugin", "add", "ledger")[0] == 1
    code, out, _ = run(capsys, "plugin", "add", "ledger", "--region", "gb")
    assert code == 0 and "region set to GB" in out
    assert profile(box)["machine"]["region"] == "GB" and profile(box)["modules"]["ledger"] is True
    assert profile(box)["machine"]["notes"] == str(box / "Notes")  # the rest of [machine] is kept
    assert run(capsys, "plugin", "add", "ledger")[0] == 0  # remembered: no flag needed now


def test_region_flag_that_does_not_fit_is_refused_and_writes_nothing(regional, capsys, monkeypatch):
    box = regional
    in_region(monkeypatch, None)
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    before = (box / ".ws" / "profile.toml").read_text()
    code, _, err = run(capsys, "plugin", "add", "ledger", "--region", "FR")
    assert code == 1 and "you said FR" in err
    code, _, err = run(capsys, "plugin", "add", "ledger", "--region", "Aotearoa")
    assert code == 1 and "two-letter" in err
    assert (box / ".ws" / "profile.toml").read_text() == before


def test_an_unknown_region_does_not_refuse(regional, capsys, monkeypatch):
    box = regional
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    in_region(monkeypatch, None)
    assert run(capsys, "plugin", "add", "ledger")[0] == 0


def test_a_utc_server_with_a_us_locale_is_warned_not_refused(regional, capsys, monkeypatch):
    box = regional
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setattr(modules, "ZONE_TAB", box / "no-zone.tab")
    code, out, err = run(capsys, "plugin", "add", "ledger")
    assert code == 0 and "ledger is on" in out
    assert "your locale says US" in err and "only a guess" in err and "--region AU" in err


def test_the_region_comes_from_the_profile_then_the_zone_then_the_locale(box, monkeypatch):
    monkeypatch.setattr(modules, "ZONE_TAB", box / "no-zone.tab")
    env = {"TZ": "Europe/Dublin", "LANG": "en_US.UTF-8", "WS_HOME": str(box / ".ws"), "HOME": str(box)}
    assert modules.region_of(env) == ("IE", "zone")
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
        ("NZST-12NZDT,M9.5.0,M4.1.0/3", "NZ"),
        ("Australia/Perth", "AU"),
        ("AEST-10AEDT,M10.1.0,M4.1.0/3", "AU"),
        ("Europe/London", "GB"),
        ("Europe/Dublin", "IE"),
        ("America/Los_Angeles", "US"),
        ("America/Toronto", "CA"),
        ("Asia/Kolkata", "IN"),
        ("Africa/Johannesburg", "ZA"),
        ("JST-9", "JP"),
        ("UTC", None),
        ("Etc/UTC", None),
        ("EST5EDT", None),  # shared by more than one country: no guess
        ("Nowhere/Land", None),
    ],
)
def test_the_zone_region_from_the_built_in_table(tz, want, tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "ZONE_TAB", tmp_path / "no-zone.tab")
    assert modules.zone_region({"TZ": tz}) == want


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"LANG": "en_CA.UTF-8"}, "CA"),
        ({"LC_ALL": "en_AU.UTF-8", "LANG": "en_GB.UTF-8"}, "AU"),
        ({"LANG": "C.UTF-8"}, None),
        ({}, None),
    ],
)
def test_the_locale_region(env, want):
    assert modules.locale_region(env) == want


def test_the_system_zone_table_wins_and_reaches_other_countries(tmp_path, monkeypatch):
    tab = tmp_path / "zone.tab"
    tab.write_text("# comment\nBR\t-2332-04637\tAmerica/Sao_Paulo\tSoutheast\nUS\t+404251-0740023\tAmerica/New_York\n")
    monkeypatch.setattr(modules, "ZONE_TAB", tab)
    assert modules.zone_region({"TZ": "America/Sao_Paulo"}) == "BR"  # not in the built-in table
    assert modules.zone_region({"TZ": "America/New_York"}) == "US"


ZONE_TAB_TEXT = "# comment\nUS\t+340308-1181434\tAmerica/Los_Angeles\tPacific\nGB\t+513030-0000731\tEurope/London\nAU\t-3352+15113\tAustralia/Sydney\n"


@pytest.mark.parametrize(
    ("tz", "want"),
    [("UTC", None), ("Europe/London", "GB"), ("America/Los_Angeles", "US"), ("Australia/Sydney", "AU")],
)
def test_a_plugin_follows_the_zone_the_test_sets_whatever_the_host_zone_is(regional, capsys, monkeypatch, tz, want):
    """The whole path, from $TZ (set here; ws reads the variable, not the C library's zone) to the plugin command."""
    box = regional
    tab = box / "zone.tab"
    tab.write_text(ZONE_TAB_TEXT)
    monkeypatch.setattr(modules, "ZONE_TAB", tab)
    monkeypatch.setattr(modules, "locale_region", lambda env: None)
    monkeypatch.setenv("TZ", tz)
    assert modules.zone_region(os.environ) == want
    assert run(capsys, "install", "--yes", "--notes", str(box / "Notes"))[0] == 0
    code, _, err = run(capsys, "plugin", "add", "ledger")
    if want == "US":
        assert code == 1 and "ledger is for AU/GB only" in err and "time zone is in US" in err
    else:
        assert code == 0
