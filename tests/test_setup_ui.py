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
from pathlib import Path

import pytest
from test_install import snapshot

from ws import cli, install
from ws.install import InstallError
from ws.setup_ui import flow, inline
from ws.setup_ui.brand import ANSI_RE, LOGO, LOGO_SMALL, LOGO_WIDTH, Ansi, visible_len, xterm_hex
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
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.chdir(home)
    return home


def fake_runner(box: Path, version: str = "2.0.0") -> None:
    exe = box.parent / "bin" / "claude"
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def plain(text: str) -> str:
    return ANSI_RE.sub("", text)


def walk(keys, *, live=False, interactive=True, region="NZ", cols=100, **flags):
    facts = flow.gather(os.environ, region=region)
    choices = flow.choices_from(facts, region=region, **flags)
    out = io.StringIO()
    it = iter(keys)

    def key():
        k = next(it, None)
        assert k is not None, "the walk asked for more keys than the test gave"
        if isinstance(k, BaseException):
            raise k
        return k

    wiz = inline.Wizard(
        facts, choices, out, key, sleep=lambda _: None, cols=cols, color=False, live=live, interactive=interactive
    )
    return wiz.run(), out.getvalue(), facts


BEGIN, ON = "enter", "enter"
ALL_ENTER = ["enter"] * 4  # begin, plugins, notes, install


# ---- the engine behind it ------------------------------------------------------------------------------------------


@posix_only
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
    for word in ("ws-aitoolkit", "Machine", "Core", "Plugins", "Notes folder", "Review", "Install", "Done", "Next"):
        assert word in t
    assert "\x1b[" not in t  # no colour and no cursor codes when not live
    assert (box / ".ws" / "profile.toml").is_file() and (box / "Notes" / "_inbox").is_dir()


def test_the_walk_picks_a_plugin_with_the_keyboard(box):
    code, text, _ = walk(["enter", "space", "enter", "enter", "enter"])
    assert code == 0 and "Mail drafts" in text
    assert "mail-drafts = true" in (box / ".ws" / "profile.toml").read_text()


def test_the_walk_will_not_choose_finance_outside_nz(box):
    keys = ["enter", "down", "down", "space", "enter", "enter", "enter"]
    walk(keys, region="US")
    assert "finance = false" in (box / ".ws" / "profile.toml").read_text()


def test_the_walk_lets_the_notes_folder_be_typed_and_asks_before_making_it(box):
    keys = ["enter", "enter", "2", "enter", "y", "enter"]  # begin, plugins, "~/Notes2", confirm the new folder, install
    code, text, _ = walk(keys, live=True)
    assert code == 0 and "does not exist" in plain(text) and (box / "Notes2" / "_inbox").is_dir()
    assert "~/Notes2" in (box / ".ws" / "profile.toml").read_text()


def test_declining_to_make_the_folder_goes_back_to_editing(box):
    pre = snapshot(box)
    code, text, _ = walk(["enter", "enter", "2", "enter", "n", "esc"], live=True)
    assert code == 1 and "Cancelled" in plain(text) and snapshot(box) == pre


def test_the_walk_can_go_back_from_the_review(box):
    code, text, _ = walk(["enter", "enter", "enter", "b", "enter", "enter", "enter"])
    assert code == 0 and text.count("Review") >= 1 and "Done" in text


def test_the_review_lists_the_files_on_demand(box):
    code, text, _ = walk(["enter", "enter", "enter", "d", "enter"], live=True)
    assert code == 0 and "policy.toml" in text and "your policy" in text


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


def test_colour_is_off_for_no_color_pipes_and_dumb_terminals(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert inline.wants_color(Tty(), {}) is True
    assert inline.wants_color(Tty(), {"NO_COLOR": "1"}) is False
    assert inline.wants_color(Tty(), {"TERM": "dumb"}) is False
    assert inline.wants_color(io.StringIO(), {}) is False
    a = Ansi(False)
    assert a.violet("x") == "x" and a.rule(5) == "━" * 5 and plain(Ansi(True).mint("ok")) == "ok"


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


def test_the_visual_kit_is_written_down():
    import ws.setup_ui as ui

    for word in ("Visual kit", "Violet", "Mint", "gradient rule", "Status block", "Hint line", "Cards", "Fallbacks"):
        assert word in ui.__doc__


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


def test_the_page_is_local_escaped_and_has_no_external_urls(box):
    facts = flow.gather(os.environ, region="NZ")
    page = render_page(facts, flow.choices_from(facts))
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "") and "https://" not in page
    assert "opt-in" not in page and "Choose your plugins" in page and 'id="boot"' in page
    boot = re.search(r'id="boot">(.*?)</script>', page, re.DOTALL).group(1)
    assert "<" not in boot and json.loads(boot)["plugins"][0]["name"] == "mail-drafts"


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


def test_install_calls_the_engine_then_the_server_closes(box):
    srv, t, out = _start()
    status, data = _api(srv, "/api/install", {"plugins": ["calendar"], "notes": "~/Notes"})
    doc = json.loads(data)
    assert status == 200 and doc["headline"].startswith("+") and doc["next"][0] == "ws doctor"
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


def test_help_reads_like_claws_usage_flags_examples(capsys):
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


def test_a_plugin_already_on_is_marked_on_now(box, capsys):
    walk(["enter", "space", "enter", "enter", "enter"])
    _, text, _ = walk(["c", "enter", "enter", "enter"])
    assert "on now" in text
