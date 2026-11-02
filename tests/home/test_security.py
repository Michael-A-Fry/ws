"""The security review of the home page: a terminal-only folder isn't started from the box, a typed answer needs `{text}`,
an old ask isn't placed on whatever session holds its window now, an answer is sent once, a template fills in one pass,
and the render file is private from its first byte."""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import threading

import pytest

from home import SID, SID2, Box, agent, iso
from ws import cli
from ws.home import api, hub, page, template
from ws.home.server import HomeServer
from ws.output import WsError

START = '[home.start]\ncommand = ["starter", "{workspace}", "{runner}", "{model}", "{request}"]\n'
ASK_ID = "a20261102T120000-aaa1"


def env_of(box, calls, windows=None, agents=(), code=0):
    def run(argv, timeout_s):
        if argv[0] == "claude":
            return (0, json.dumps(list(agents))) if agents else (1, "")
        if argv[0] == "tmux":
            return (0, windows) if windows is not None else (1, "")
        calls.append(argv)
        return code, ""

    return box.env(run=run)


def put_ask(box, **more):
    line = {"event": "added", "id": ASK_ID, "ts": iso(-30), "repo": "app", "question": "Q?",
            "session": {"tmux": "ws:@1"}, "redacted": False, "options": [{"label": "A"}], **more}  # fmt: skip
    p = box.usage / "asks" / "asks.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")


# --- 1: a terminal-only folder -----------------------------------------------------------------------------------------


def test_a_folder_under_opaque_paths_cannot_be_started_from_the_page_with_any_runner(tmp_path):
    b = Box(tmp_path)
    private = b.home / "code" / "private"
    b.profile(f'[home]\nopaque_paths = ["{private}"]\n' + START + '[home.models]\nopencode = ["qwen"]\n')
    b.ws_session(SID, cwd=str(private / "app"))
    calls: list = []
    env = env_of(b, calls)
    h = hub.build(api.snapshot(env), env)
    (w,) = h.workspaces
    assert w["cls"] == hub.OPAQUE_CLASS
    for runner, model in (("opencode", "qwen"), ("claude-code", "")):
        code, body = hub.start(env, {"what": "session", "workspace": w["key"], "where": "testhost", "runner": runner,
                                     "model": model, "request": "look"})  # fmt: skip
        assert code == 409 and "terminal only" in body["error"]
    assert calls == []  # nothing ran


def test_the_box_greys_every_runner_out_for_a_terminal_only_folder_and_says_why(tmp_path):
    b = Box(tmp_path)
    private = b.home / "code" / "private"
    b.profile(f'[home]\nopaque_paths = ["{private}"]\n' + START + '[home.models]\nopencode = ["qwen"]\n')
    b.ws_session(SID, cwd=str(private / "app"))
    env = env_of(b, [])
    snap = api.snapshot(env)
    out = page.render(snap, live=True, hub=hub.build(snap, env))
    models = out.split('id="c-runner"')[1].split("</select>")[0]
    assert models.count(" disabled") == models.count("<option") > 0
    assert "terminal only: start it from a terminal" in models


def test_an_opaque_folder_and_an_open_one_with_the_same_name_stay_two_workspaces(tmp_path):
    b = Box(tmp_path)
    private = b.home / "code" / "private"
    start = '[home.start]\ncommand = ["starter", "{workspace}", "{folder}", "{runner}", "{model}", "{request}"]\n'
    b.profile(f'[home]\nopaque_paths = ["{private}"]\n' + start)  # {folder}: a short name alone could mean either
    b.ws_session(SID, cwd=str(private / "app"))
    b.ws_session(SID2, cwd=str(b.home / "code" / "app"))
    calls: list = []
    env = env_of(b, calls)
    h = hub.build(api.snapshot(env), env)
    assert sorted(w["cls"] for w in h.workspaces) == ["", hub.OPAQUE_CLASS]
    assert len({w["key"] for w in h.workspaces}) == 2
    open_ws = next(w for w in h.workspaces if not w["cls"])
    assert open_ws["name"] == "code/app"  # two share a name, so the parent folder tells them apart
    good = {"what": "session", "where": "testhost", "runner": "claude-code", "model": "", "request": "go"}
    assert hub.start(env, {**good, "workspace": open_ws["key"]})[0] == 200
    shut = next(w for w in h.workspaces if w["cls"])
    assert hub.start(env, {**good, "workspace": shut["key"]})[0] == 409
    assert hub.start(env, {**good, "workspace": "app"})[0] == 409  # a bare name that two folders share isn't a choice
    assert calls == [["starter", "app", str(b.home / "code" / "app"), "claude-code", "", "go"]]


# --- 2: a typed answer needs {text} ------------------------------------------------------------------------------------


def test_a_typed_answer_is_refused_when_the_template_has_no_text(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{choice}"]\n')
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    put_ask(b)
    calls: list = []
    env = env_of(b, calls, agents=[agent(SID, cwd=str(b.home / "code" / "app"))])
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, text="no, stop", env=env)
    assert e.value.kind == "refused" and "{text}" in e.value.hint and calls == []
    api.answer(ASK_ID, choice="A", env=env)
    assert calls == [["ws-ask", "answer", ASK_ID, "A"]]


def test_a_choice_is_refused_when_the_template_has_only_text(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{text}"]\n')
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    put_ask(b)
    env = env_of(b, [], agents=[agent(SID, cwd=str(b.home / "code" / "app"))])
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, choice="A", env=env)
    assert e.value.kind == "refused" and "{choice}" in e.value.hint


def test_an_answer_template_with_neither_choice_nor_text_is_refused_at_load(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}"]\n')
    with pytest.raises(WsError) as e:
        api.Env.make(b.environ, now=0.0, host="h")
    assert e.value.kind == "bad_config" and "{choice} or {text}" in e.value.message


def test_the_form_hides_what_the_template_cannot_carry(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{choice}"]\n')
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    put_ask(b)
    env = env_of(b, [], agents=[agent(SID, cwd=str(b.home / "code" / "app"))])
    snap = api.snapshot(env)
    out = page.render(snap, live=True, hub=hub.build(snap, env))
    assert 'data-action="ask.choose"' in out and "Something else" not in out and 'data-action="ask.send"' not in out


# --- 3: an old ask isn't placed on a newer session ----------------------------------------------------------------------


def _windowed(b, started_s, windows="ws:2 @1\n", ask_tmux="ws:2", **ask_more):
    b.pid_file(1001, SID, tmux="ws:@1.%1")
    put_ask(b, ts=iso(-86000), session={"tmux": ask_tmux}, question="secret q", **ask_more)
    a = agent(SID, cwd=str(b.home / "code" / "app"), startedAt=int((_now() + started_s) * 1000))
    return env_of(b, [], windows=windows, agents=[a])


def _now() -> float:
    from home import NOW

    return NOW


def test_an_old_ask_is_not_placed_on_the_session_that_holds_its_window_number_now(tmp_path):
    b = Box(tmp_path)
    env = _windowed(b, started_s=-3600, session_name="builder")  # the ask is 24 h old; the session began 1 h ago
    (a,) = api.asks(env)
    assert a.opaque is True and a.title != "secret q"


def test_a_bare_window_number_with_no_names_is_not_a_match(tmp_path):
    b = Box(tmp_path)
    env = _windowed(b, started_s=-90000)  # the session is older than the ask, but nothing names either side
    (a,) = api.asks(env)
    assert a.opaque is True


def test_a_restarted_tmux_server_reusing_a_window_id_does_not_place_the_old_ask(tmp_path):
    b = Box(tmp_path)
    b.pid_file(1001, SID, tmux="ws:@1.%1")
    put_ask(b, ts=iso(-7200), session={"tmux": "ws:@1"})  # asked 2 h ago on window @1
    new = agent(SID, cwd=str(b.home / "code" / "app"), startedAt=int((_now() - 600) * 1000))  # a session of 10 min ago
    (a,) = api.asks(env_of(b, [], agents=[new]))
    assert a.opaque is True
    older = agent(SID, cwd=str(b.home / "code" / "app"), startedAt=int((_now() - 86400) * 1000))
    (a,) = api.asks(env_of(b, [], agents=[older]))
    assert a.opaque is False and a.title == "Q?"  # the same window, a session that was there when it was asked


# --- 4: an answer is sent once -----------------------------------------------------------------------------------------


def _post(srv, body):
    h = {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": f"http://127.0.0.1:{srv.port}",
         "Host": f"127.0.0.1:{srv.port}"}  # fmt: skip
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request("POST", "/api/answer", json.dumps(body), h)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


@pytest.fixture
def served(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{choice}"]\n')
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    put_ask(b)
    calls: list = []
    started = threading.Event()
    release = threading.Event()

    def run(argv, timeout_s):
        if argv[0] == "claude":
            return 0, json.dumps([agent(SID, cwd=str(b.home / "code" / "app"))])
        if argv[0] == "tmux":
            return 1, ""
        calls.append(argv)
        started.set()
        release.wait(5)
        return 0, ""

    srv = HomeServer(lambda: b.env(run=run), idle_seconds=30)
    threading.Thread(target=srv.serve, daemon=True).start()
    yield srv, calls, started, release
    release.set()
    srv.close()


def test_an_ask_already_being_answered_is_refused_the_second_time(served):
    srv, calls, started, release = served
    first: list = []
    t = threading.Thread(target=lambda: first.append(_post(srv, {"id": ASK_ID, "choice": "A"})))
    t.start()
    assert started.wait(5)
    status, body = _post(srv, {"id": ASK_ID, "choice": "A"})  # a second tab, while the first is still running
    assert status == 409 and "already" in body["error"]
    release.set()
    t.join(5)
    assert first == [(200, {"ok": True})]
    assert _post(srv, {"id": ASK_ID, "choice": "A"})[0] == 409  # and after it finished
    assert len(calls) == 1


def test_a_timeout_says_the_answer_may_have_gone_and_is_not_offered_again(tmp_path):
    b = Box(tmp_path)
    b.profile('[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{choice}"]\n')
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    put_ask(b)

    def run(argv, timeout_s):
        if argv[0] == "claude":
            return 0, json.dumps([agent(SID, cwd=str(b.home / "code" / "app"))])
        if argv[0] == "tmux":
            return 1, ""
        raise TimeoutError("took too long")

    srv = HomeServer(lambda: b.env(run=run), idle_seconds=30)
    threading.Thread(target=srv.serve, daemon=True).start()
    try:
        status, body = _post(srv, {"id": ASK_ID, "choice": "A"})
        assert status == 504 and body["may_have_sent"] is True
        assert _post(srv, {"id": ASK_ID, "choice": "A"})[0] == 409
    finally:
        srv.close()
    assert "may have been sent" in page.SCRIPT_LIVE  # the page says so, and doesn't re-arm the form


# --- 5: templates ------------------------------------------------------------------------------------------------------


def test_a_dash_is_refused_on_the_finished_word_wherever_the_value_sits():
    with pytest.raises(ValueError, match="dash"):
        template.fill(["p", "{model}{request}"], {"model": "", "request": "-rf"})
    with pytest.raises(ValueError, match="dash"):
        template.fill(["p", "{a}{b}"], {"a": "-", "b": "x"})
    assert template.fill(["p", "--flag={request}"], {"request": "-x"}) == ["p", "--flag=-x"]  # the template's own dash
    assert template.fill(["p", "x{request}"], {"request": "-x"}) == ["p", "x-x"]


def test_a_value_that_holds_a_name_is_not_filled_in_again():
    got = template.fill(["p", "{request}{workspace}"], {"request": "{workspace}", "workspace": "secret"})
    assert got == ["p", "{workspace}secret"]


# --- 6: the render file ------------------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_render_file_is_private_when_it_is_made_and_when_it_was_already_there(tmp_path, monkeypatch, capsys):
    b = Box(tmp_path)
    monkeypatch.setenv("HOME", str(b.home))
    monkeypatch.setenv("WS_HOME", str(b.ws))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("TMUX", raising=False)
    modes: list[int] = []
    real = os.open

    def spy(path, flags, mode=0o777, **kw):
        if str(path).endswith("home.html"):
            modes.append(mode)
            assert flags & os.O_CREAT and flags & os.O_TRUNC
        return real(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    target = tmp_path / "home.html"
    assert cli.main(["home", "--render", str(target)]) == 0
    assert modes == [0o600] and stat.S_IMODE(os.stat(target).st_mode) == 0o600
    target.chmod(0o644)
    assert cli.main(["home", "--render", str(target)]) == 0
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    capsys.readouterr()
