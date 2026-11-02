"""`ws home` and `ws home --render FILE`: the command, the file it writes and the server it starts."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from home import SID, Box
from ws import cli, output
from ws.home import cli as home_cli
from ws.home import page, server


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path)
    b.ws_session(SID, cwd=str(b.home / "code" / "app"))
    monkeypatch.setenv("HOME", str(b.home))
    monkeypatch.setenv("WS_HOME", str(b.ws))
    monkeypatch.setenv("PATH", "")  # no `claude`, no `tmux`: the sources that need them say so
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("WS_OUTPUT", raising=False)
    return b


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_writes_the_page_to_a_private_file(box, capsys, tmp_path):
    target = tmp_path / "out" / "home.html"
    target.parent.mkdir()
    code, out, _ = run(capsys, "home", "--render", str(target))
    assert code == 0 and str(target) in out
    text = target.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "<title>ws home</title>" in text
    assert "data-opener" not in text and "/api/open" not in text  # a file has no server behind it
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_render_under_json_is_one_envelope(box, capsys, tmp_path):
    target = tmp_path / "home.html"
    code, out, _ = run(capsys, "home", "--render", str(target), "--json")
    env = json.loads(out)
    assert code == 0 and env["schema"] == output.SCHEMA and env["kind"] == "home" and env["status"] == "ok"
    assert env["data"]["path"] == str(target) and env["data"]["bytes"] == target.stat().st_size


def test_render_with_a_bad_profile_is_a_typed_error(box, capsys, tmp_path):
    box.profile("[home]\nnope = 1\n")
    code, out, _ = run(capsys, "home", "--render", str(tmp_path / "x.html"), "--json")
    env = json.loads(out)
    assert code != 0 and env["status"] == "error" and env["error_kind"] == "bad_config" and "nope" in env["message"]
    assert not (tmp_path / "x.html").exists()


def test_render_shows_the_records_the_api_found(box, capsys, tmp_path):
    box.profile('[home]\nlead = "name:*"\n')
    target = tmp_path / "home.html"
    run(capsys, "home", "--render", str(target))
    text = target.read_text(encoding="utf-8")
    assert "app" in text and "Pinned" in text and "No lead session" not in text
    assert "collie" in text  # the sources list says what was and wasn't read


def test_data_is_still_its_own_command_and_a_json_envelope(box, capsys):
    code, out, _ = run(capsys, "home", "data", "--json")
    assert code == 0 and json.loads(out)["data"]["schema"] == "ws.home/1"


def test_a_bare_home_starts_the_server_and_no_browser_is_a_flag(box, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(server, "run", lambda open_browser=True: seen.append(open_browser) or 0)
    assert cli.main(["home"]) == 0 and cli.main(["home", "--no-browser"]) == 0
    assert seen == [True, False]
    assert home_cli.server.run is server.run


def test_the_server_prints_a_loopback_address_and_the_launcher_goes_when_it_closes(box, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)

    def stop(self):
        self.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(server.HomeServer, "serve", stop)
    assert server.run() == 0
    out = capsys.readouterr().out
    assert out.startswith("ws home: http://127.0.0.1:") and "?t=" in out
    url = out.split()[2]
    assert (
        len(opened) == 1 and opened[0].startswith("file://") and url not in opened
    )  # the token isn't on a command line
    launcher = opened[0].removeprefix("file://")
    assert not os.path.exists(launcher)


def test_a_bad_profile_stops_the_server_before_it_binds(box):
    box.profile("[home]\nnope = 1\n")
    from ws.output import WsError

    with pytest.raises(WsError):
        server.run(open_browser=False)


def test_the_shared_script_holds_the_look_and_the_view_and_only_a_live_page_posts():
    assert "ws-home-look" in page.SCRIPT and "ws-home-view" in page.SCRIPT and "fetch(" not in page.SCRIPT
    for path in ("/api/open", "/api/answer", "/api/start"):
        assert path in page.SCRIPT_LIVE and path not in page.SCRIPT
    assert "X-Ws-Token" in page.SCRIPT_LIVE and page.SCRIPT in page.SCRIPT_LIVE
    assert "UNDO_MS = 6000" in page.SCRIPT_LIVE  # an answer is sent after a 6 second undo window


def test_the_page_works_at_phone_width():
    assert "@media (max-width:600px)" in page.CSS and "overflow-wrap" in page.CSS
    assert "minmax(min(300px,100%),1fr)" in page.CSS  # an eval card never forces a sideways scroll
