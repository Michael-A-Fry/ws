"""ws setup: the inline walk and the web page, both over the real install engine. Everything lives under a tmp HOME and
WS_HOME; a fake `claude` on PATH stands in for the runner."""

from __future__ import annotations

import ast
import http.client
import io
import json
import os
import re
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest
from test_install import snapshot

from ws import brand, cli, install, modules
from ws.brand import ANSI_RE, BASIC, FULL, LOGO, LOGO_SMALL, LOGO_WIDTH, Ansi, visible_len, xterm_hex
from ws.install import InstallError
from ws.setup_ui import flow, inline
from ws.setup_ui.web import SetupServer, render_page

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake runner is a shell script")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Notes").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv(modules.SHOW_UNBUILT, raising=False)  # the default view: built plugins only
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.chdir(home)
    return home


@pytest.fixture
def unbuilt(box, monkeypatch):
    """The developer view: plugins still being built are offered, so the choosing, region and merge rules can be walked."""
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    return box


def fake_runner(box: Path, version: str = "2.0.0") -> None:
    exe = box.parent / "bin" / "claude"
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def plain(text: str) -> str:
    return ANSI_RE.sub("", text)


def walk(keys, *, live=False, interactive=True, region="NZ", cols=100, cap=20, color=False, **flags):
    """Walk the steps with these keys. A cap is given by default, so the optional cap step is not asked."""
    facts = flow.gather(os.environ, region=region)
    choices = flow.choices_from(facts, region=region, cap=cap, **flags)
    out = io.StringIO()
    it = iter(keys)

    def key():
        k = next(it, None)
        assert k is not None, "the walk asked for more keys than the test gave"
        if isinstance(k, BaseException):
            raise k
        return k

    wiz = inline.Wizard(
        facts, choices, out, key, sleep=lambda _: None, cols=cols, color=color, live=live, interactive=interactive
    )
    return wiz.run(), out.getvalue(), facts


BEGIN, ON = "enter", "enter"
ALL_ENTER = ["enter"] * 4  # begin, plugins, notes, install


# ---- the engine behind it ------------------------------------------------------------------------------------------


@posix_only
@pytest.mark.usefixtures("unbuilt")
def test_a_second_setup_reports_0_changes_and_uninstall_leaves_no_residue(box, capsys):
    fake_runner(box)
    pre = snapshot(box)
    assert cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "calendar"]) == 0
    first = plain(capsys.readouterr().out)
    assert "Done" in first and "+" in first and "Everything is set up" not in first
    after = snapshot(box)
    assert after != pre

    assert cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "calendar"]) == 0
    second = plain(capsys.readouterr().out)
    assert "Everything is set up" in second and "0 changes" in second
    assert snapshot(box) == after

    assert cli.main(["uninstall", "--yes"]) == 0
    capsys.readouterr()
    assert snapshot(box) == pre


@posix_only
def test_setup_goes_through_install_apply_so_the_manifest_has_it(box, capsys):
    fake_runner(box)
    cli.main(["setup", "--yes", "--region", "NZ"])
    entries = json.loads(install.manifest_path(os.environ).read_text())["entries"]
    paths = {Path(e["path"]) for e in entries}
    assert box / ".ws" / "profile.toml" in paths and box / ".ws" / "policy.toml" in paths
    assert box / "Notes" / "_inbox" in paths


def test_the_status_block_is_the_real_machine(box):
    fake_runner(box, "9.8.7")
    (box / "Obsidian" / "V" / ".obsidian").mkdir(parents=True)
    (box / ".ws").mkdir()
    (box / ".ws" / "profile.toml").write_text('[roles.budget]\nmonthly_cap_usd = 25\n[roles.model]\ndefault = "m-1"\n')
    facts = flow.gather(os.environ, region="NZ")
    rows = {r.label: r for r in flow.status_rows(facts)}
    assert rows["Runner"].value == "Claude Code 9.8.7" and rows["Runner"].tone == "ok"
    assert rows["Budget"].value == "US$0.00 of US$25 this month"
    assert rows["Model"].value == "m-1"
    assert "Obsidian vault found" in rows["Notes"].value and rows["Notes"].tone == "ok"
    assert rows["Machine"].value.endswith("· NZ")


def test_no_runner_found_is_said_plainly_and_nothing_breaks(box, capsys):
    facts = flow.gather(os.environ, region="NZ")
    rows = {r.label: r for r in flow.status_rows(facts)}
    assert rows["Runner"].tone == "warn" and "none found" in rows["Runner"].value
    assert cli.main(["setup", "--yes", "--region", "NZ"]) == 0
    assert "Runner   none yet" in plain(capsys.readouterr().out)


@pytest.mark.usefixtures("unbuilt")
def test_the_region_rule_is_the_real_one(box, capsys):
    nz, us = flow.gather(os.environ, region="NZ"), flow.gather(os.environ, region="US")
    assert {p.name: p.available for p in nz.plugins}["finance"] is True
    assert {p.name: p.available for p in us.plugins}["finance"] is False
    with pytest.raises(InstallError, match="NZ only"):
        flow.check_choices(us, flow.choices_from(us, plugins=["finance"]))
    pre = snapshot(box)
    assert cli.main(["setup", "--yes", "--region", "US", "--plugin", "finance"]) == 1
    assert "NZ only" in capsys.readouterr().err and snapshot(box) == pre
    assert cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "finance"]) == 0


def test_a_region_given_goes_into_the_profile_and_the_diff(box, capsys):
    assert cli.main(["setup", "--yes", "--region", "nz"]) == 0
    assert 'region = "NZ"' in (box / ".ws" / "profile.toml").read_text()


@pytest.mark.usefixtures("unbuilt")
def test_the_preview_writes_nothing_and_counts_like_the_install(box):
    facts = flow.gather(os.environ, region="NZ")
    pre = snapshot(box)
    pv = flow.preview(facts, flow.choices_from(facts, plugins=["calendar"]))
    assert snapshot(box) == pre
    assert pv.headline().startswith(f"+{len(pv.new_files)} files, 0 changed")
    out = flow.run_install(facts, flow.choices_from(facts, plugins=["calendar"]), pv.steps)
    assert len(out.result.created) == len(pv.result.created)
    assert flow.preview(facts, flow.choices_from(facts, plugins=["calendar"])).headline() == "0 changes"


def test_bad_answers_are_refused_before_anything_is_written(box, capsys):
    pre = snapshot(box)
    for argv, message in (
        (["--plugin", "nope"], "unknown plugin"),
        (["--cap", "0"], "above 0"),
        (["--region", "N"], "two-letter"),
        (["--notes", str(box)], "too broad"),
    ):
        assert cli.main(["setup", "--yes", *argv]) == 1
        assert message in capsys.readouterr().err
    assert snapshot(box) == pre


def test_apply_calls_progress_after_each_step_and_not_on_a_dry_run(box):
    facts = flow.gather(os.environ, region="NZ")
    steps = install.plan(flow.answers_for(facts, flow.choices_from(facts)), os.environ, facts.osi)
    seen = []
    install.apply(steps, os.environ, dry_run=True, progress=lambda s, a: seen.append(a))
    assert seen == []
    install.apply(steps, os.environ, progress=lambda s, a: seen.append((s.path, a)))
    assert [p for p, _ in seen] == [s.path for s in steps] and {a for _, a in seen} <= {
        "created",
        "changed",
        "unchanged",
    }


def test_progress_groups_the_steps_and_reports_each_group_once(box):
    facts = flow.gather(os.environ, region="NZ")
    steps = install.plan(flow.answers_for(facts, flow.choices_from(facts)), os.environ, facts.osi)
    prog = flow.Progress(steps)
    finished = []
    install.apply(steps, os.environ, progress=lambda s, a: finished.append(prog.step(s, a)))
    assert [n for n in finished if n] == prog.names
    assert "Profile and policy" in prog.names and prog.detail("Profile and policy") == "+2 files"


# ---- the inline walk -------------------------------------------------------------------------------------------------


def test_the_walk_takes_the_defaults_and_installs(box):
    code, text, _ = walk(ALL_ENTER)
    t = text
    assert code == 0
    for word in ("ws-aitoolkit", "Machine", "Core", "Notes folder", "Review", "Install", "Done", "Next"):
        assert word in t
    assert "\x1b[" not in t  # no colour and no cursor codes when not live
    assert (box / ".ws" / "profile.toml").is_file() and (box / "Notes" / "_inbox").is_dir()


@pytest.mark.usefixtures("unbuilt")
def test_the_walk_picks_a_plugin_with_the_keyboard(box):
    code, text, _ = walk(["enter", "space", "enter", "enter", "enter"])
    assert code == 0 and "Mail drafts" in text
    assert "mail-drafts = true" in (box / ".ws" / "profile.toml").read_text()


def test_the_walk_will_not_choose_finance_outside_nz(box):
    keys = ["enter", "down", "down", "space", "enter", "enter", "enter"]
    walk(keys, region="US")
    assert "finance = false" in (box / ".ws" / "profile.toml").read_text()


@pytest.mark.usefixtures("unbuilt")
def test_the_walk_lets_the_notes_folder_be_typed_and_asks_before_making_it(box):
    keys = ["enter", "enter", "2", "enter", "y", "enter"]  # begin, plugins, "~/Notes2", confirm the new folder, install
    code, text, _ = walk(keys, live=True)
    assert code == 0 and "does not exist" in plain(text) and (box / "Notes2" / "_inbox").is_dir()
    assert "~/Notes2" in (box / ".ws" / "profile.toml").read_text()


@pytest.mark.usefixtures("unbuilt")
def test_declining_to_make_the_folder_goes_back_to_editing(box):
    pre = snapshot(box)
    code, text, _ = walk(["enter", "enter", "2", "enter", "n", "esc"], live=True)
    assert code == 1 and "Cancelled" in plain(text) and snapshot(box) == pre


def test_the_walk_can_go_back_from_the_review(box):
    code, text, _ = walk(["enter", "enter", "enter", "b", "enter", "enter", "enter"])
    assert code == 0 and text.count("Review") >= 1 and "Done" in text


@pytest.mark.usefixtures("unbuilt")
def test_the_review_lists_the_files_on_demand(box):
    code, text, _ = walk(["enter", "enter", "enter", "d", "enter"], live=True)
    assert code == 0 and "policy.toml" in text and "your policy" in text


@pytest.mark.usefixtures("unbuilt")
@pytest.mark.parametrize(
    "keys",
    [
        ["q"],  # the welcome
        ["enter", "q"],  # the plugins
        ["enter", "enter", "esc"],  # the notes folder
        ["enter", "enter", "enter", "q"],  # the review
        ["enter", "enter", "enter", KeyboardInterrupt()],  # Ctrl-C at the review
    ],
)
def test_cancelling_at_any_step_writes_nothing(box, keys):
    pre = snapshot(box)
    try:
        code, text, _ = walk(keys)
        assert code == 1 and "Nothing was written" in text
    except KeyboardInterrupt:
        pass
    assert snapshot(box) == pre


def test_ctrl_c_during_the_install_stops_cleanly_and_uninstall_removes_what_was_made(box, monkeypatch, capsys):
    pre = snapshot(box)
    real = flow.run_install
    calls = []

    def interrupted(facts, c, steps, on_step=None):
        def wrapped(step, action):
            calls.append(step.path)
            on_step(step, action)
            if len(calls) == 3:
                raise KeyboardInterrupt

        return real(facts, c, steps, wrapped)

    monkeypatch.setattr(flow, "run_install", interrupted)
    code, text, _ = walk(ALL_ENTER)
    assert code == 130 and "Stopped" in text and "ws uninstall" in text
    assert snapshot(box) != pre
    assert cli.main(["uninstall", "--yes"]) == 0
    capsys.readouterr()
    assert snapshot(box) == pre


def test_a_set_up_machine_opens_on_everything_is_set_up(box):
    walk(ALL_ENTER)
    code, text, facts = walk(["enter"])
    assert code == 0 and "Everything is set up" in text and "0 changes" in text
    assert "ws plugin add" in text and "ws uninstall" in text
    assert facts.set_up is True


@pytest.mark.usefixtures("unbuilt")
def test_changing_something_on_a_set_up_machine_walks_the_steps_again(box):
    walk(ALL_ENTER)
    code, text, _ = walk(["c", "space", "enter", "enter", "enter"])  # c, plugins: pick one, notes, install
    assert code == 0 and "Plugins" in text and "Done" in text
    assert "mail-drafts = true" in (box / ".ws" / "profile.toml").read_text()


def test_the_walk_with_nothing_to_change_says_so(box):
    walk(ALL_ENTER)
    code, text, _ = walk(["c", "enter", "enter", "enter"])
    assert code == 0 and "0 changes" in text and "Nothing to do" in text


def test_live_drawing_redraws_in_place_and_ends_with_the_cursor_back(box):
    code, text, _ = walk(ALL_ENTER, live=True)
    assert code == 0 and text.startswith(inline.HIDE) and text.endswith(inline.SHOW)
    assert "\x1b[2K" in text and any(f in text for f in ("⠋", "⠙", "⠹"))


def test_the_mark_folds_under_80_columns(box):
    _, wide, _ = walk(["q"], cols=120)
    _, small, _ = walk(["q"], cols=70)
    assert LOGO[0].strip() in wide and "Your memory" in wide
    assert LOGO[0].strip() not in small and LOGO_SMALL[0].strip() in small and "Your memory" not in small


class Tty(io.StringIO):
    def isatty(self):
        return True


class FakeStdin:
    def isatty(self):
        return True

    def fileno(self):
        return 0


class FakeTerminal:
    """Stands in for the real terminal: records whether the cursor codes were asked for."""

    made: ClassVar[list] = []

    def __init__(self, fd, out, *, cursor=True):
        self.cursor, self.pending = cursor, False
        FakeTerminal.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def attended_run(monkeypatch, env, *, no_color=False):
    """`inline.run` on a pretend terminal with these environment variables; returns (code, output, terminal)."""
    FakeTerminal.made = []
    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(inline, "Terminal", FakeTerminal)
    keys = iter(ALL_ENTER)
    monkeypatch.setattr(inline, "Keys", lambda fd: type("K", (), {"read": staticmethod(lambda: next(keys))})())
    facts = flow.gather(os.environ, region="NZ")
    out = Tty()
    code = inline.run(facts, flow.choices_from(facts, region="NZ", cap=20), out=out, no_color=no_color, env=env)
    return code, out.getvalue(), FakeTerminal.made[0]


def test_a_dumb_terminal_is_asked_the_same_questions_but_nothing_is_redrawn(box, monkeypatch):
    code, text, term = attended_run(monkeypatch, {"TERM": "dumb"})
    assert code == 0 and term.cursor is False
    assert "\x1b" not in text  # no colour, no cursor-move, no erase, no hide or show
    assert "Review" in text and "Done" in text


def test_forced_colour_on_a_dumb_terminal_is_colour_only(box, monkeypatch):
    code, text, term = attended_run(monkeypatch, {"TERM": "dumb", "FORCE_COLOR": "1"})
    codes = ANSI_RE.findall(text)
    assert code == 0 and term.cursor is False and codes
    assert all(c.endswith("m") for c in codes), "only colour codes: nothing that moves the cursor or erases"


def test_a_terminal_that_can_redraw_does(box, monkeypatch):
    code, text, term = attended_run(monkeypatch, {"TERM": "xterm-256color"})
    assert code == 0 and term.cursor is True
    assert "\x1b[?25l" in text and "\x1b[2K" in text


@pytest.mark.parametrize(
    ("env", "flag", "colour"),
    [
        ({}, False, True),
        ({"COLORFGBG": "0;15"}, False, True),
        ({"NO_COLOR": "1"}, False, False),
        ({"NO_COLOR": ""}, False, True),  # an empty NO_COLOR is not a request
        ({}, True, False),  # --no-color
        ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, False, False),
    ],
)
def test_colour_follows_no_color_the_flag_and_force_color(box, monkeypatch, env, flag, colour):
    _, text, _ = attended_run(monkeypatch, {"TERM": "xterm", **env}, no_color=flag)
    assert bool(re.search(r"\x1b\[[0-9;]*m", text)) is colour


def test_the_no_color_flag_is_accepted_by_ws_setup(box, capsys):
    assert cli.main(["setup", "--yes", "--no-color", "--region", "NZ"]) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_with_colour_stripped_the_output_is_the_no_color_output(box):
    """The same walk three ways: stripping the codes from either coloured run gives the NO_COLOR text."""
    texts = {}
    for name, color in (("full", FULL), ("basic", BASIC), ("off", False)):
        code, texts[name], _ = walk(ALL_ENTER, color=color)
        assert code == 0
        assert cli.main(["uninstall", "--yes"]) == 0
    assert "\x1b[" in texts["full"] and "\x1b[" in texts["basic"] and "\x1b" not in texts["off"]
    assert plain(texts["full"]) == texts["off"] == plain(texts["basic"])


def test_setup_needs_a_terminal_without_yes(box, capsys):
    assert cli.main(["setup", "--region", "NZ"]) == 1
    assert "needs a terminal" in capsys.readouterr().err


def test_native_windows_is_refused_by_name(box, capsys, monkeypatch):
    from ws import osinfo

    win = osinfo.OSInfo("windows", "11", "amd64", "none", False)
    monkeypatch.setattr(osinfo, "detect", lambda *a, **k: win)
    assert cli.main(["setup", "--yes"]) == 1
    assert "WSL2" in capsys.readouterr().err


def test_termios_is_only_imported_inside_the_function_that_needs_it():
    """`ws --help` and `ws doctor` must load on native Windows, which has no termios."""
    top = ast.parse(Path(inline.__file__).read_text()).body
    names = {a.name for n in top if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not names & {"termios", "tty", "curses"}
    assert "termios" in Path(inline.__file__).read_text()


def test_keys_are_read_one_at_a_time_even_when_they_arrive_together(tmp_path):
    r, w = os.pipe()
    try:
        os.write(w, b"\x1b[A\x1b[B\x1b\r \x7f\xc3\xa9\x1bOA\x1b[15~")
        keys = inline.Keys(r)
        got = [keys.read() for _ in range(9)]
    finally:
        os.close(r)
        os.close(w)
    assert got == ["up", "down", "esc", "enter", "space", "backspace", "é", "up", "other"]


def test_brand_colours_mark_and_helpers():
    assert xterm_hex(99) == "#875fff" and xterm_hex(49) == "#00ffaf" and xterm_hex(214) == "#ffaf00"
    assert len({len(r.ljust(LOGO_WIDTH)) for r in LOGO}) == 1 and len(LOGO) == 6 and len(LOGO_SMALL) == 3
    assert visible_len(Ansi(True).violet("abc", bold=True)) == 3
    assert [plain(r) for r in Ansi(True).logo()] == [r.ljust(LOGO_WIDTH) for r in LOGO]


@posix_only
def test_the_visual_kit_describes_what_the_code_draws(box):
    """The kit in `ws.setup_ui`'s docstring, checked against the output rather than against its own words."""
    fake_runner(box)
    full = Ansi(FULL)
    facts = flow.gather(os.environ, region="NZ")
    banner = "\n".join(inline.banner(full, 100, 120))
    assert "\x1b[1mws-aitoolkit" in banner  # the name is bold, with no colour of its own
    assert "\x1b[1;38;5;49msetup" in banner  # the word "setup" is mint
    assert banner.count("38;5;99m") >= 4 and "38;5;63m" in banner and "38;5;43m" in banner  # the mark and the rule
    assert "38;5;99" in inline.hints(full, ("↵", "begin"))  # the key in a hint is violet
    assert "\x1b[1;38;5;214m~" in "\n".join(  # a changed file is amber, an added one mint
        inline.review_rows(
            full,
            facts,
            flow.choices_from(facts),
            type(
                "P",
                (),
                {"headline": lambda s: "+1", "rows": [{"kind": "file", "action": "change", "path": "p", "what": "w"}]},
            )(),
            details=True,
            room=5,
            collapsed=True,
        )
    )
    ok = [r for r in flow.status_rows(facts) if r.tone == "ok"]
    assert ok and "\x1b[38;5;49m✓" in "\n".join(inline.status(full, facts))  # a found fact gets a mint tick


def test_the_terminal_uses_only_the_colours_the_kit_names(box):
    """Two accents and a few greys: every 256-colour number in a coloured walk is a token's number."""
    _, text, _ = walk(ALL_ENTER, color=FULL)
    used = {int(n) for n in re.findall(r"38;5;(\d+)", text)}
    assert used and used <= {t.xterm for t in brand.TOKENS if t.xterm is not None}
    assert {99, 49, 245} <= used  # violet, mint and the quiet grey are all there


def test_a_refusal_is_red_and_a_warning_amber(box):
    for color, red, amber in ((FULL, "38;5;203", "38;5;214"), (BASIC, "31", "33")):
        facts = flow.gather(os.environ, region="NZ")
        out = io.StringIO()
        wiz = inline.Wizard(facts, flow.choices_from(facts), out, lambda: "q", color=color, live=False)
        wiz.fail("no", "hint")
        assert f"\x1b[1;{red}m✗" in out.getvalue()
        assert f"\x1b[{amber}m" in Ansi(color).amber("x")


# ---- the web page ------------------------------------------------------------------------------------------------------


def _start(**kw):
    facts = flow.gather(os.environ, region="NZ")
    srv = SetupServer(facts, flow.choices_from(facts, region="NZ"), **kw)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(result=srv.serve()), daemon=True)
    t.start()
    return srv, t, out


def _call(srv, method, path, body=None, headers=None, origin=True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    if origin and method == "POST":
        h.setdefault("Origin", f"http://127.0.0.1:{srv.port}")
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def _api(srv, path, body, **kw):
    headers = {"X-Ws-Token": srv.token, **kw.pop("headers", {})}
    return _call(srv, "POST", path, json.dumps(body), headers, **kw)


@pytest.mark.usefixtures("unbuilt")
def test_the_page_is_local_escaped_and_has_no_external_urls(box):
    facts = flow.gather(os.environ, region="NZ")
    page = render_page(facts, flow.choices_from(facts))
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "") and "https://" not in page
    assert "opt-in" not in page and "Choose your plugins" in page and 'id="boot"' in page
    boot = re.search(r'id="boot">(.*?)</script>', page, re.DOTALL).group(1)
    assert "<" not in boot and json.loads(boot)["plugins"][0]["name"] == "mail-drafts"


def _unbuilt_words() -> set[str]:
    """Every way a plugin that is not built could be named on screen: its name and its setup label, in lower case."""
    out = set()
    for name, m in modules.REGISTRY.items():
        if not m.built:
            out |= {name, flow.PLUGIN_TEXT.get(name, (name, ""))[0].lower()}
    return out


def test_setup_offers_only_plugins_that_are_built(box):
    assert _unbuilt_words()
    facts = flow.gather(os.environ, region="NZ")
    assert facts.plugins == []
    page = render_page(facts, flow.choices_from(facts)).lower()
    boot = json.loads(re.search(r'id="boot">(.*?)</script>', page, re.DOTALL).group(1))
    assert boot["plugins"] == []
    for word in _unbuilt_words():
        assert word not in page


@pytest.mark.parametrize("live", [False, True])
def test_the_walk_shows_no_plugin_that_is_not_built(box, live):
    code, text, facts = walk(["enter", "enter", "enter"], live=live)  # begin, notes, install
    assert code == 0 and facts.plugins == []
    low = plain(text).lower()
    assert "plugins" not in low.replace("none yet (ws plugin add later)", "")
    for word in _unbuilt_words():
        assert word not in low


def test_an_unbuilt_plugin_is_not_a_choice_by_name(box, capsys):
    facts = flow.gather(os.environ, region="NZ")
    with pytest.raises(flow.ChoiceError, match="unknown plugin"):
        flow.check_choices(facts, flow.choices_from(facts, plugins=["finance"]))
    assert cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "calendar"]) == 1
    assert "unknown plugin" in capsys.readouterr().err
    assert not (box / ".ws" / "profile.toml").exists()


def test_a_profile_with_an_unbuilt_plugin_on_loads_keeps_it_and_doctor_notes_it(box, capsys, monkeypatch):
    monkeypatch.setenv(modules.SHOW_UNBUILT, "1")
    assert cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "calendar", "--cap", "20"]) == 0
    monkeypatch.delenv(modules.SHOW_UNBUILT)
    capsys.readouterr()
    facts = flow.gather(os.environ, region="NZ")
    assert facts.plugins == [] and "calendar" in facts.base.modules
    assert cli.main(["setup", "--yes", "--region", "NZ", "--cap", "20"]) == 0  # run again: the choice is not dropped
    assert "calendar = true" in (box / ".ws" / "profile.toml").read_text()
    capsys.readouterr()
    code = cli.main(["doctor", "--offline", "--json"])
    checks = [c for c in json.loads(capsys.readouterr().out)["data"]["checks"] if c["id"] == "WS009"]
    assert code in (0, 1) and len(checks) == 1 and checks[0]["status"] == "warn"
    assert "calendar" in checks[0]["detail"] and "plugin remove calendar" in checks[0]["fix"]


def test_doctor_has_no_plugin_line_when_none_is_unbuilt_and_on(box, capsys):
    assert cli.main(["setup", "--yes", "--region", "NZ", "--cap", "20"]) == 0
    capsys.readouterr()
    cli.main(["doctor", "--offline", "--json"])
    assert not [c for c in json.loads(capsys.readouterr().out)["data"]["checks"] if c["id"] == "WS009"]


@pytest.mark.usefixtures("unbuilt")
def test_the_page_shows_the_real_region_rule(box):
    facts = flow.gather(os.environ, region="US")
    boot = re.search(r'id="boot">(.*?)</script>', render_page(facts, flow.choices_from(facts)), re.DOTALL).group(1)
    finance = next(p for p in json.loads(boot)["plugins"] if p["name"] == "finance")
    assert finance["available"] is False and "NZ" in finance["why"]


def test_the_server_is_loopback_only_and_checks_token_host_and_origin(box):
    srv, t, _ = _start()
    try:
        assert srv.httpd.server_address[0] == "127.0.0.1"
        assert _call(srv, "GET", f"/?t={srv.token}")[0] == 200
        assert _call(srv, "GET", "/?t=wrong")[0] == 403 and _call(srv, "GET", "/")[0] == 403
        assert _call(srv, "GET", f"/?t={srv.token}", headers={"Host": "example.test"})[0] == 403
        assert _call(srv, "GET", f"/other?t={srv.token}")[0] == 404
        body = {"plugins": [], "notes": "~/Notes"}
        assert _api(srv, "/api/preview", body)[0] == 200
        assert _api(srv, "/api/preview", body, headers={"X-Ws-Token": "wrong"})[0] == 403
        # the token in the URL is not enough for the API: it must be in the header
        assert _call(srv, "POST", f"/api/preview?t={srv.token}", json.dumps(body))[0] == 403
        assert _api(srv, "/api/preview", body, origin=False)[0] == 403  # no Origin: not our page
        assert _api(srv, "/api/preview", body, headers={"Origin": "http://evil.test"}, origin=False)[0] == 403
        assert _api(srv, "/api/preview", body, headers={"Host": "example.test"})[0] == 403
        assert _api(srv, "/api/preview", body, headers={"Content-Type": "text/plain"})[0] == 415
        assert _api(srv, "/api/nothing", body)[0] == 404
    finally:
        srv.close()
        t.join(5)


def test_the_api_rejects_a_bad_body_and_a_refused_choice_without_writing(box):
    srv, t, _ = _start()
    pre = snapshot(box)
    try:
        assert _call(srv, "POST", "/api/preview", "not json", {"X-Ws-Token": srv.token})[0] == 400
        assert _api(srv, "/api/preview", {"plugins": "mail-drafts"})[0] == 400
        assert _api(srv, "/api/preview", {"plugins": ["nope"]})[0] == 400
        status, data = _api(srv, "/api/install", {"plugins": [], "notes": str(box)})
        assert status == 400 and "too broad" in json.loads(data)["error"]
    finally:
        srv.close()
        t.join(5)
    assert snapshot(box) == pre


@pytest.mark.usefixtures("unbuilt")
def test_the_preview_counts_and_writes_nothing(box):
    srv, t, _ = _start()
    pre = snapshot(box)
    try:
        status, data = _api(srv, "/api/preview", {"plugins": ["calendar"], "notes": "~/Notes"})
        doc = json.loads(data)
        assert status == 200 and doc["headline"].startswith("+") and doc["changes"] > 0
        assert any(r["path"].endswith("profile.toml") for r in doc["rows"])
    finally:
        srv.close()
        t.join(5)
    assert snapshot(box) == pre


@pytest.mark.usefixtures("unbuilt")
def test_install_calls_the_engine_then_the_server_closes(box):
    srv, t, out = _start()
    status, data = _api(srv, "/api/install", {"plugins": ["calendar"], "notes": "~/Notes"})
    doc = json.loads(data)
    assert (
        status == 200
        and doc["headline"].startswith("+")
        and doc["next"][0] == ["ws doctor", "check that everything works"]
    )
    assert doc["groups"][0]["name"] == "Profile and policy"
    t.join(5)
    assert not t.is_alive() and out["result"]["headline"] == doc["headline"]
    assert "calendar = true" in (box / ".ws" / "profile.toml").read_text()
    assert install.manifest_path(os.environ).is_file()
    with pytest.raises(OSError):
        _call(srv, "GET", f"/?t={srv.token}")
    # what it wrote is what `ws install` would have: a second install changes nothing
    facts = flow.gather(os.environ, region="NZ")
    assert flow.preview(facts, flow.choices_from(facts)).headline() == "0 changes"


@pytest.mark.usefixtures("unbuilt")
def test_a_refused_region_is_a_400_with_the_reason(box):
    facts = flow.gather(os.environ, region="US")
    srv = SetupServer(facts, flow.choices_from(facts))
    threading.Thread(target=srv.serve, daemon=True).start()
    try:
        status, data = _api(srv, "/api/install", {"plugins": ["finance"], "notes": "~/Notes"})
        assert status == 400 and "NZ only" in json.loads(data)["error"]
    finally:
        srv.close()


def test_finish_closes_a_set_up_page_and_idle_closes_the_rest(box):
    srv, t, out = _start()
    assert _api(srv, "/api/finish", {})[0] == 200
    t.join(5)
    assert out["result"]["headline"] == "0 changes"
    facts = flow.gather(os.environ, region="NZ")
    assert SetupServer(facts, flow.choices_from(facts), idle_seconds=0.3).serve() is None


def test_setup_help_lists_every_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["setup", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--web", "--yes", "--plugin", "--notes", "--runner", "--cap", "--region", "--no-open"):
        assert flag in out


@pytest.mark.usefixtures("unbuilt")
def test_errors_end_with_a_line_that_says_what_to_try(box, capsys):
    for argv, hint in (
        (["--plugin", "nope"], "ws plugin list"),
        (["--cap", "0"], "--cap 20"),
        (["--region", "N"], "--region NZ"),
        (["--region", "US", "--plugin", "finance"], "--region NZ"),
    ):
        assert cli.main(["setup", "--yes", *argv]) == 1
        err = capsys.readouterr().err.splitlines()
        assert err[0].startswith("ws: ") and hint in err[1]


def test_help_reads_as_usage_flags_examples(capsys):
    with pytest.raises(SystemExit):
        cli.main(["setup", "--help"])
    out = capsys.readouterr().out
    assert out.index("Usage:") < out.index("Flags:") < out.index("Examples:")
    assert "Everything is set up: 0 changes" in out and "ws setup --yes" in out


@posix_only
def test_the_close_says_where_the_answers_are_saved(box, capsys):
    fake_runner(box)
    assert cli.main(["setup", "--yes", "--region", "NZ"]) == 0
    out = capsys.readouterr().out
    assert "Saved in ~/.ws/profile.toml" in out and "Next" in out


@pytest.mark.usefixtures("unbuilt")
def test_a_plugin_already_on_is_marked_on_now(box, capsys):
    walk(["enter", "space", "enter", "enter", "enter"])
    _, text, _ = walk(["c", "enter", "enter", "enter"])
    assert "on now" in text


# ---- review round 1: input that is not plain, signals, the token, the cap ------------------------------------------------

ESC = "\x1b"
OSC = f"{ESC}]0;pwned\x07"


def test_clean_removes_every_control_character():
    assert flow.clean(f"a{OSC}b\x85c\x7fd\ne\tf\x00g") == "a]0;pwnedbcdefg"
    assert flow.has_control("x\x9b") and not flow.has_control("naïve ✓ ~/Notes")


def test_nothing_from_outside_reaches_the_terminal_raw(box):
    fake_runner(box, f"9.9.9{OSC}")  # what the runner prints for --version
    (box / ".ws").mkdir()
    toml = (
        '[roles.model]\ndefault = "m\\u001b]0;pwned\\u0007x"\n'  # TOML escapes: the file holds the text, not a raw ESC
    )
    (box / ".ws" / "profile.toml").write_text(toml)
    (box / "Obsidian" / f"V{ESC}[31m" / ".obsidian").mkdir(parents=True)  # a vault whose folder name has an ESC in it
    facts = flow.gather(os.environ, region="NZ")
    rows = {r.label: r.value for r in flow.status_rows(facts)}
    assert not any(flow.has_control(v) for v in rows.values())
    assert facts.vaults == []  # not offered: a name with a control character cannot be chosen by typing it
    _, text, _ = walk(ALL_ENTER, live=True)
    assert f"{ESC}]" not in text and "\x07" not in text  # no OSC: no window title, no bell
    assert all(not flow.has_control(c) or c in "\n\r" for c in set(ANSI_RE.sub("", text)))  # only ws's own escapes


def test_a_notes_folder_must_be_plain_and_a_full_path(box, capsys):
    (box / "elsewhere").mkdir()
    (box / "linkhome").symlink_to(box, target_is_directory=True)
    pre = snapshot(box)
    for notes, message in (
        ("Notes", "must start with"),  # relative
        ("~root/x", "must start with"),  # another user's home
        ("~", "must start with"),
        (f"~/a{ESC}b", "control character"),
        ("~/a\x00b", "control character"),
        ("~/a\x85b", "control character"),
        (str(box / "linkhome"), "too broad"),  # a link to your home does not get past the check
        ("/", "too broad"),
        ("", "empty"),
        ("~/" + "x" * 5000, "too long"),
    ):
        facts = flow.gather(os.environ, region="NZ")
        c = replace(flow.choices_from(facts, region="NZ"), notes=notes)
        with pytest.raises(InstallError, match=message):
            flow.check_choices(facts, c)
    assert snapshot(box) == pre


def test_the_cli_refuses_a_bad_notes_folder_with_a_hint(box, capsys):
    pre = snapshot(box)
    for notes, hint in (("Notes", "~/Notes"), ("~root/x", "~/Notes"), (f"~/a{ESC}b", "plain path")):
        assert cli.main(["setup", "--yes", "--notes", notes]) == 1
        err = capsys.readouterr().err.splitlines()
        assert err[0].startswith("ws: ") and hint in err[1]
        assert ESC not in "".join(err)
    assert snapshot(box) == pre


def test_a_step_that_could_fail_half_way_is_refused_before_the_first_write(box):
    with pytest.raises(InstallError, match="control character"):
        flow.check_steps([install.Step(box / "a\x00b", "file", "x")])
    with pytest.raises(InstallError, match="cannot be written"):
        flow.check_steps([install.Step(box / "ok", "file", "bad \ud800 text")])


@posix_only
def test_the_web_refuses_a_nul_in_the_notes_folder_without_writing_and_stays_up(box):
    srv, t, _ = _start()
    pre = snapshot(box)
    try:
        for notes in ("\u0000x", "~/a\u0000b", f"~/a{ESC}b", "relative"):
            for path in ("/api/preview", "/api/install"):
                status, data = _api(srv, path, {"plugins": [], "notes": notes})
                assert status == 400 and json.loads(data)["error"]
        assert snapshot(box) == pre  # not even the policy or the profile
        assert _api(srv, "/api/preview", {"plugins": [], "notes": "~/Notes"})[0] == 200  # still answering
    finally:
        srv.close()
        t.join(5)


def test_the_web_handler_never_raises_whatever_goes_wrong(box, monkeypatch):
    srv, t, _ = _start()
    body = {"plugins": [], "notes": "~/Notes"}
    try:
        with monkeypatch.context() as m:  # a context, not monkeypatch.undo(): that would drop box's HOME too
            m.setattr(flow, "preview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
            status, data = _api(srv, "/api/preview", body)
        doc = json.loads(data)
        assert status == 500 and "went wrong" in doc["error"] and "boom" not in doc["error"]
        real = flow.run_install

        def broken(*a, **k):
            real(*a, **k)
            raise ValueError("late")

        monkeypatch.setattr(flow, "run_install", broken)
        status, data = _api(srv, "/api/install", body)
        assert status == 400 and "ws uninstall" in json.loads(data)["error"]  # it says what state things are in
        monkeypatch.setattr(flow, "run_install", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        status, data = _api(srv, "/api/install", body)
        assert status == 500 and "recorded" in json.loads(data)["error"]
    finally:
        monkeypatch.undo()
        srv.close()
        t.join(5)


def test_a_non_ascii_token_is_a_403_not_a_traceback(box, capsys):
    srv, t, _ = _start()
    try:
        assert _call(srv, "GET", "/?t=%C3%A9")[0] == 403
        status, _ = _call(srv, "POST", "/api/preview", "{}", {"X-Ws-Token": "t\xe9k\xe9n"})
        assert status == 403
        assert _call(srv, "GET", f"/?t={srv.token}")[0] == 200  # still up
    finally:
        srv.close()
        t.join(5)
    assert "Traceback" not in capsys.readouterr().err


def test_only_a_request_with_the_token_keeps_the_page_alive(box):
    import time

    facts = flow.gather(os.environ, region="NZ")
    srv = SetupServer(facts, flow.choices_from(facts), idle_seconds=0.6)
    done = threading.Event()
    threading.Thread(target=lambda: (srv.serve(), done.set()), daemon=True).start()
    end = time.monotonic() + 3
    while not done.is_set() and time.monotonic() < end:
        try:
            _call(srv, "GET", "/?t=wrong")  # a stranger knocking does not reset the idle clock
        except OSError:
            break
        time.sleep(0.1)
    assert done.wait(3)


def test_the_browser_is_started_on_a_private_file_not_on_the_token(box, monkeypatch, capsys):
    from ws.setup_ui import web

    seen: dict = {}
    made: ClassVar[list] = []

    class Spy(SetupServer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(web, "SetupServer", Spy)
    monkeypatch.setattr(web.webbrowser, "open", lambda arg: seen.setdefault("arg", arg))
    facts = flow.gather(os.environ, region="NZ")
    t = threading.Thread(target=lambda: seen.setdefault("code", web.run(facts, flow.choices_from(facts))), daemon=True)
    t.start()
    import time

    for _ in range(100):
        if "arg" in seen:
            break
        time.sleep(0.05)
    srv = made[0]
    arg = seen["arg"]
    assert arg.startswith("file://") and srv.token not in arg
    launcher = Path(arg.removeprefix("file://"))
    assert srv.token in launcher.read_text()  # the token is in the private file, which only you can read
    if sys.platform != "win32":
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o600 and stat.S_IMODE(launcher.parent.stat().st_mode) == 0o700
    assert _call(srv, "GET", f"/?t={srv.token}")[0] == 200
    assert not launcher.exists()  # gone after the first load
    assert _api(srv, "/api/finish", {})[0] == 200
    t.join(5)
    out = capsys.readouterr().out
    assert "open this link again" in out and seen["code"] == 0


def test_the_403_page_says_what_to_do(box):
    srv, t, _ = _start()
    try:
        status, body = _call(srv, "GET", "/")
        assert status == 403 and b"link that ws setup printed" in body
    finally:
        srv.close()
        t.join(5)


@posix_only
def test_sigterm_in_raw_mode_restores_the_terminal_and_cancels(box):
    import pty
    import signal
    import termios
    import time

    master, slave = pty.openpty()
    out = io.StringIO()
    before = termios.tcgetattr(slave)[3]
    try:
        with pytest.raises(inline.Cancelled), inline.Terminal(slave, out):
            assert not termios.tcgetattr(slave)[3] & termios.ICANON  # single-key mode while it runs
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
        kernel = getattr(termios, "PENDIN", 0)  # macOS sets PENDIN itself; it is not ours to restore
        assert termios.tcgetattr(slave)[3] & ~kernel == before & ~kernel  # put back
        assert out.getvalue().startswith(inline.HIDE) and out.getvalue().endswith(inline.SHOW)
        assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler) or callable(
            signal.getsignal(signal.SIGTERM)
        )
    finally:
        os.close(master)
        os.close(slave)


@posix_only
def test_ctrl_z_gives_the_screen_back_and_fg_takes_it_again(box):
    import pty
    import termios

    master, slave = pty.openpty()
    out = io.StringIO()
    try:
        term = inline.Terminal(slave, out)
        term.enter()
        assert not termios.tcgetattr(slave)[3] & termios.ICANON and out.getvalue().endswith(inline.HIDE)
        term.leave()  # what the Ctrl-Z handler does before it stops the process
        assert termios.tcgetattr(slave)[3] & termios.ICANON and out.getvalue().endswith(inline.SHOW)
        term.enter()  # and what SIGCONT does
        assert not termios.tcgetattr(slave)[3] & termios.ICANON and out.getvalue().endswith(inline.HIDE)
        term.leave()
    finally:
        os.close(master)
        os.close(slave)


def test_the_width_is_read_again_so_a_resize_is_picked_up(box):
    size = [120]
    facts = flow.gather(os.environ, region="NZ")
    wiz = inline.Wizard(facts, flow.choices_from(facts), io.StringIO(), lambda: "enter", cols=lambda: size[0])
    assert (wiz.cols, wiz.width) == (120, 100)
    size[0] = 70  # the window was resized
    assert (wiz.cols, wiz.width) == (70, 66)


def test_the_cap_step_is_asked_only_when_there_is_no_cap_and_skips_by_default(box):
    keys = ["enter", "enter", "enter", "enter", "enter"]  # begin, plugins, notes, cap (skip), install
    code, text, _ = walk(keys, cap=None, live=True)
    assert (
        code == 0
        and "Monthly cap" in plain(text)
        and "monthly_cap_usd" not in (box / ".ws" / "profile.toml").read_text()
    )


@pytest.mark.usefixtures("unbuilt")
def test_a_cap_typed_in_the_walk_goes_into_the_profile(box):
    keys = ["enter", "enter", "enter", "2", "0", "enter", "enter"]
    code, text, _ = walk(keys, cap=None, live=True)
    assert code == 0 and "monthly_cap_usd = 20" in (box / ".ws" / "profile.toml").read_text()
    assert "US$20 a month" in plain(text)


@pytest.mark.usefixtures("unbuilt")
def test_a_bad_cap_is_said_plainly_and_asked_again(box):
    keys = ["enter", "enter", "enter", "0", "enter", "backspace", "5", "enter", "enter"]
    code, text, _ = walk(keys, cap=None, live=True)
    assert (
        code == 0
        and "number above 0" in plain(text)
        and "monthly_cap_usd = 5" in (box / ".ws" / "profile.toml").read_text()
    )


def test_the_cap_step_is_not_asked_when_the_profile_or_a_flag_has_one(box):
    code, text, _ = walk(ALL_ENTER, cap=30)
    assert code == 0 and "Monthly cap" not in text
    code, text, _ = walk(ALL_ENTER)  # the profile now has 30
    assert "Monthly cap" not in text


def test_the_web_takes_an_optional_cap_and_refuses_one_that_is_not_a_number(box):
    facts = flow.gather(os.environ, region="NZ")
    srv = SetupServer(facts, flow.choices_from(facts))
    threading.Thread(target=srv.serve, daemon=True).start()
    try:
        assert flow.parse_cap("") is None and flow.parse_cap("US$ 12.5") == 12.5
        for bad in ("abc", "0", "-3", "nan", "inf", "1e400"):
            status, _ = _api(srv, "/api/preview", {"plugins": [], "notes": "~/Notes", "cap": bad})
            assert status == 400, bad
        assert _api(srv, "/api/preview", {"plugins": [], "notes": "~/Notes", "cap": True})[0] == 400
        status, _ = _api(srv, "/api/install", {"plugins": [], "notes": "~/Notes", "cap": "20"})
        assert status == 200
        assert "monthly_cap_usd = 20" in (box / ".ws" / "profile.toml").read_text()
    finally:
        srv.close()
    page = render_page(facts, flow.choices_from(facts))
    assert "Monthly cap (US$)" in page


@pytest.mark.usefixtures("unbuilt")
def test_setup_never_writes_dial_act(box, capsys):
    from ws import policy

    cli.main(["setup", "--yes", "--region", "NZ", "--plugin", "calendar", "--cap", "20"])
    for name in ("policy.toml", "profile.toml"):
        text = (box / ".ws" / name).read_text()
        assert 'dial = "act"' not in text
    assert policy.loads((box / ".ws" / "policy.toml").read_text()).dial == "prepare"
    # and no code path in setup sets a dial at all
    src = "".join(p.read_text() for p in Path(inline.__file__).parent.glob("*.py"))
    assert not re.search(r"\bdial\b", src)


@posix_only
def test_a_sigterm_while_installing_is_noted_and_the_summary_says_what_was_written(box, monkeypatch):
    import pty
    import signal

    master, slave = pty.openpty()
    real = flow.run_install

    def term_then_install(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM)  # lands while the install is writing
        return real(*a, **k)

    monkeypatch.setattr(flow, "run_install", term_then_install)
    try:
        facts = flow.gather(os.environ, region="NZ")
        out = io.StringIO()
        keys = iter(ALL_ENTER)
        with inline.Terminal(slave, out) as term:
            wiz = inline.Wizard(
                facts,
                flow.choices_from(facts, cap=20),
                out,
                lambda: next(keys),
                sleep=lambda _: None,
                color=False,
                live=False,
                terminal=term,
            )
            code = wiz.run()
        text = out.getvalue()
        assert code == 130 and term.pending
        assert "Done" in text and "Nothing was written" not in text  # what was written is reported, not denied
        assert (box / ".ws" / "profile.toml").is_file()
    finally:
        os.close(master)
        os.close(slave)


# ---- the page's look: tokens, motion, policy -----------------------------------------------------------------------


def test_the_page_colours_come_from_the_tokens(box):
    from ws.setup_ui import web

    facts = flow.gather(os.environ, region="NZ")
    page = render_page(facts, flow.choices_from(facts))
    assert brand.css() in page
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", web.STYLE), "the rules name roles; the tokens hold the colours"
    assert not re.search(r"#[0-9a-fA-F]{6}\b", web.JS)
    assert "--add" not in page and "--faint" not in page  # the old hand-typed names are gone
    for i, role in enumerate(brand.GRADIENT_ROLES):  # the mark: a class per row, no inline colour
        assert f'<i class="r{i}">' in page and f".logo .r{i}{{color:var(--{role})}}" in page
    assert "<i style" not in page and 'style="' not in page


def test_the_spinner_stops_for_people_who_ask_for_reduced_motion(box):
    from ws.setup_ui import web

    block = re.search(r"@media\(prefers-reduced-motion:reduce\)\{(.*?)\}\}?\n", web.CSS, re.DOTALL)
    assert block and "animation:none" in block.group(1) and "transition:none" in block.group(1)
    assert "animation:turn" in web.CSS  # the spinner it switches off


def _get_page(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", f"/?t={srv.token}")
    r = conn.getresponse()
    body = r.read().decode()
    csp = r.getheader("Content-Security-Policy")
    conn.close()
    return csp, body


def test_each_response_gets_its_own_nonce_and_the_policy_has_no_unsafe_inline(box):
    srv, t, _ = _start()
    try:
        csp1, page1 = _get_page(srv)
        csp2, page2 = _get_page(srv)
        for csp, page in ((csp1, page1), (csp2, page2)):
            nonce = re.search(r"script-src 'nonce-([\w-]+)'", csp).group(1)
            assert f"style-src 'nonce-{nonce}'" in csp and "unsafe-inline" not in csp
            assert f'<style nonce="{nonce}">' in page and f'<script nonce="{nonce}">' in page
        assert csp1 != csp2
        status, _ = _call(srv, "GET", "/nothing?t=" + srv.token)
        assert status == 404
    finally:
        srv.close()
        t.join(5)


def test_a_json_reply_is_allowed_nothing(box):
    from ws.setup_ui import web

    assert web.csp() == "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def test_the_finished_screen_has_a_description_for_every_command(box):
    facts = flow.gather(os.environ, region="NZ")
    steps = flow.next_steps(facts, flow.choices_from(facts))
    assert [c for c, _ in steps] == flow.next_commands(facts, flow.choices_from(facts))
    assert all(cmd.startswith("ws ") and desc for cmd, desc in steps)
    page = render_page(facts, flow.choices_from(facts))
    assert "text: 'ws-aitoolkit'" not in page and "h('b', {text: 'ws'})" in page


def test_the_setup_server_is_the_shared_loopback_server_with_setups_three_calls(box):
    from ws.ui.server import LoopbackServer

    facts = flow.gather(os.environ, region="NZ")
    srv = SetupServer(facts, flow.choices_from(facts))
    try:
        assert isinstance(srv, LoopbackServer)
        assert set(srv.get_routes) == {"/"} and set(srv.post_routes) == {"/api/preview", "/api/install", "/api/finish"}
    finally:
        srv.httpd.server_close()
