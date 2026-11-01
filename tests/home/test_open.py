"""Opening a session or an ask where it lives: which opener offers what, what an opaque item is offered, and the one call
the server may run."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from home import SID, Box, agent
from ws.home import api
from ws.home import open as opener
from ws.home.openers import Ctx, discover
from ws.home.records import Ask, Session
from ws.home.server import HomeServer

TMUX = "ws:@1.%1"
RC = "https://claude.ai/code/session_abc"


def sess(**kw) -> Session:
    base = {"id": SID, "source": "claude_code", "name": "builder", "tmux": TMUX, "cwd": "/work/app", "repo": "app",
            "state": "working"}  # fmt: skip
    base.update(kw)
    return Session(**base)


def ask(**kw) -> Ask:
    base = {"id": "a1", "source": "asks_jsonl", "kind": "question", "tmux": "ws:2", "repo": "app", "session_id": SID}
    base.update(kw)
    return Ask(**base)


def names(opens) -> list[str]:
    return [o["opener"] for o in opens]


# --- the opener modules -----------------------------------------------------------------------------------------------


def test_the_openers_are_found_by_scanning_the_folder():
    mods = discover()
    assert [m.NAME for m in mods] == ["claude_app", "browser", "terminal", "command"]
    for m in mods:
        assert isinstance(m.OPAQUE_OK, bool) and callable(m.available) and callable(m.action)


@pytest.mark.parametrize("mod", discover(), ids=lambda m: m.NAME)
def test_opaque_items_offer_only_opaque_ok_openers(mod):
    """Whatever the profile picks, an opaque record gets an opener only if the opener says it is safe for one."""
    template = "command: ws-term {tmux}"
    pick = template if mod.NAME == "command" else mod.NAME
    for rec in (sess(opaque=True, rc_url=RC, cwd=""), ask(opaque=True, rc_url=RC)):
        got = opener.opens_for(rec, {"opaque": pick, "session": pick, "ask": pick}, {"TMUX": "/tmp/x"})
        assert (names(got) != []) == mod.OPAQUE_OK, mod.NAME
        assert all(o["opener"] in {m.NAME for m in discover() if m.OPAQUE_OK} for o in got)


def test_an_opaque_item_with_nothing_picked_gets_the_terminal_opener_only():
    got = opener.opens_for(ask(opaque=True, rc_url=RC), {}, {})
    assert names(got) == ["terminal"] and got[0]["text"] == "tmux attach -t ws:2"


def test_a_session_with_nothing_picked_gets_every_opener_that_has_something():
    got = opener.opens_for(sess(rc_url=RC), {}, {})
    assert names(got) == ["claude_app", "terminal"]  # the browser link is the same address, so it is not repeated
    assert got[0]["href"] == RC and got[1]["text"] == f"tmux attach -t {TMUX}"


def test_claude_app_offers_only_the_link_the_source_gave_and_never_builds_one():
    assert opener.opens_for(sess(), {"session": "claude_app"}, {}) == []
    assert opener.opens_for(sess(rc_url="https://example.test/x"), {"session": "claude_app"}, {}) == []
    assert opener.opens_for(sess(rc_url="https://evilclaude.ai/x"), {"session": "claude_app"}, {}) == []
    assert (
        opener.opens_for(sess(rc_url="claude://session/1"), {"session": "claude_app"}, {})[0]["href"]
        == "claude://session/1"
    )
    assert names(opener.opens_for(sess(rc_url="https://example.test/x"), {"session": "browser"}, {})) == ["browser"]
    assert opener.opens_for(sess(rc_url="javascript:alert(1)"), {"session": "browser"}, {}) == []


def test_the_terminal_opener_switches_inside_tmux_and_shows_the_attach_line_outside():
    inside = opener.opens_for(sess(), {"session": "terminal"}, {"TMUX": "/tmp/tmux-1/default,1,0"})[0]
    assert inside["post"] is True and inside["text"] == f"tmux switch-client -t {TMUX}"
    outside = opener.opens_for(sess(), {"session": "terminal"}, {})[0]
    assert "post" not in outside and outside["text"] == f"tmux attach -t {TMUX}"


@pytest.mark.parametrize("target", ["", "-t evil", "--x", "a b", "a;b", "$(x)", "x" * 200])
def test_a_tmux_target_that_is_not_a_plain_name_offers_nothing(target):
    assert opener.opens_for(sess(tmux=target), {"session": "terminal"}, {"TMUX": "1"}) == []


def test_the_command_opener_fills_the_template_and_uses_no_shell():
    pick = {"session": "command: ws-term {tmux} --cwd {cwd}"}
    got = opener.opens_for(sess(), pick, {})
    assert names(got) == ["command"] and got[0]["post"] and got[0]["label"] == "Open with ws-term"
    from ws.home.openers import command

    assert command.argv(sess(cwd="/a b/c;d"), Ctx(arg="ws-term {tmux} --cwd {cwd}")) == [
        "ws-term",
        TMUX,
        "--cwd",
        "/a b/c;d",
    ]
    assert command.argv(sess(), Ctx(arg="x {session_id}")) == ["x", SID]
    assert command.argv(ask(), Ctx(arg="x {session_id}")) == ["x", SID]  # an ask gives its session's id, not its own
    assert command.argv(sess(cwd="-rf"), Ctx(arg="rm {cwd}")) is None  # a value can't be read as an option
    assert command.argv(sess(cwd=""), Ctx(arg="x {cwd}")) is None
    assert command.argv(sess(), Ctx(arg="'unclosed")) is None
    assert opener.opens_for(sess(), {"session": "command"}, {}) == []  # named without a program: nothing to run


def test_the_command_opener_for_an_opaque_item_never_gets_a_folder():
    pick = {"opaque": "command: ws-term {tmux} {cwd}"}
    assert opener.opens_for(ask(opaque=True), pick, {}) == []
    assert names(opener.opens_for(ask(opaque=True), {"opaque": "command: ws-term {tmux}"}, {})) == ["command"]


def test_an_ended_session_is_offered_nothing():
    assert opener.opens_for(sess(state="ended", rc_url=RC), {}, {"TMUX": "1"}) == []


def test_the_profile_text_is_read_as_names_or_a_command():
    assert opener.parse("") == [] and opener.parse("  ") == []
    assert opener.parse("terminal") == [("terminal", "")]
    assert opener.parse("claude_app, terminal") == [("claude_app", ""), ("terminal", "")]
    assert opener.parse("command: ws-term {tmux}") == [("command", "ws-term {tmux}")]


# --- attach, and the call the server may run ---------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    b.ws_session(SID, cwd=str(b.home / "code" / "app"))
    return b


def _env(box, agents=None, run=None, tmux=True, profile=""):
    if tmux:
        box.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
    if profile:
        box.profile(profile)
    return box.env(
        [agent(sid=SID, cwd=str(box.home / "code" / "app")) | {"tmux": TMUX}] if agents is None else agents, run
    )


def test_attach_fills_the_opens_of_every_session_and_the_lead(box):
    box.pid_file(4242, SID, tmux=TMUX)
    env = _env(box, profile='[home]\nlead = "name:builder"\n')
    snap = opener.attach(api.snapshot(env), env)
    s = next(s for s in snap.sessions if s.id == SID)
    assert names(s.opens) == ["terminal"] and snap.lead is not None and snap.lead.opens == s.opens
    assert all(isinstance(o, dict) for o in s.opens)
    json.dumps(snap.to_dict())  # still plain JSON


def _calls(box, **kw):
    calls: list[list[str]] = []

    def run(argv, timeout_s):
        if argv[:2] == ["claude", "agents"]:
            return 0, json.dumps([agent(sid=SID, cwd=str(box.home / "code" / "app"))])
        calls.append(argv)
        return kw.get("code", 0), ""

    return run, calls


def test_perform_runs_only_a_call_that_is_on_offer_for_that_record(box):
    box.pid_file(4242, SID, tmux=TMUX)
    run, calls = _calls(box)
    env = _env(box, run=run)
    assert opener.perform(env, "terminal", "session", SID) == (200, {"ok": True})
    assert calls == [["tmux", "switch-client", "-t", TMUX]]
    assert opener.perform(env, "browser", "session", SID)[0] == 409  # not offered for it
    assert opener.perform(env, "command", "session", SID)[0] == 409  # no program set
    assert opener.perform(env, "terminal", "session", "nope")[0] == 404
    assert opener.perform(env, "terminal", "folder", SID)[0] == 400
    assert len(calls) == 1


def test_perform_outside_tmux_has_no_call_to_run(box):
    box.pid_file(4242, SID, tmux=TMUX)
    run, calls = _calls(box)
    env = _env(box, run=run, tmux=False)
    assert opener.perform(env, "terminal", "session", SID)[0] == 409 and calls == []


def test_perform_says_when_the_program_fails_or_is_missing(box):
    box.pid_file(4242, SID, tmux=TMUX)
    run, _ = _calls(box, code=1)
    assert opener.perform(_env(box, run=run), "terminal", "session", SID)[0] == 502

    def missing(argv, timeout_s):
        if argv[:2] == ["claude", "agents"]:
            return 0, json.dumps([agent(sid=SID, cwd=str(box.home / "code" / "app"))])
        raise FileNotFoundError(argv[0])

    status, doc = opener.perform(_env(box, run=missing), "terminal", "session", SID)
    assert status == 502 and "didn't run" in doc["error"]


def test_perform_for_an_opaque_ask_runs_only_the_terminal_call(box):
    box.ws.mkdir(parents=True, exist_ok=True)
    box.profile(f'[home]\nopaque_paths = ["{box.home / "code" / "app"}"]\n[home.open]\nopaque = "claude_app"\n')
    box.pid_file(4242, SID, tmux=TMUX)
    run, calls = _calls(box)
    env = _env(box, run=run)
    snap = opener.attach(api.snapshot(env), env)
    s = next(s for s in snap.sessions if s.id == SID)
    assert s.opaque and s.opens == ()  # claude_app isn't safe for an opaque item, so the profile's pick offers nothing
    assert opener.perform(env, "claude_app", "session", s.id)[0] == 409 and calls == []


# --- the server --------------------------------------------------------------------------------------------------------


def _req(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read().decode(), dict(r.getheaders())
    finally:
        conn.close()


@pytest.fixture
def served(box):
    box.pid_file(4242, SID, tmux=TMUX)
    run, calls = _calls(box)
    srv = HomeServer(lambda: _env(box, run=run), idle_seconds=30)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    yield srv, calls
    srv.close()
    t.join(5)


def _post(srv, body, **kw):
    h = {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": f"http://127.0.0.1:{srv.port}"}
    h.update(kw)
    return _req(srv, "POST", "/api/open", json.dumps(body), {k: v for k, v in h.items() if v is not None})


def test_the_home_page_is_served_with_the_token_a_nonce_and_a_strict_policy(served):
    srv, _ = served
    status, body, headers = _req(srv, "GET", f"/?t={srv.token}")
    assert status == 200 and "<title>ws home</title>" in body and 'data-opener="terminal"' in body
    nonce = headers["Content-Security-Policy"].split("style-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in body and "unsafe-inline" not in headers["Content-Security-Policy"]
    assert _req(srv, "GET", "/")[0] == 403
    assert json.loads(_req(srv, "GET", f"/api/home?t={srv.token}")[1])["schema"] == "ws.home/1"


def test_the_open_call_needs_the_token_the_origin_and_the_host(served):
    srv, calls = served
    good = {"opener": "terminal", "kind": "session", "id": SID}
    assert _post(srv, good)[0] == 200 and len(calls) == 1
    assert _post(srv, good, **{"X-Ws-Token": None})[0] == 403
    assert _post(srv, good, **{"X-Ws-Token": "wrong"})[0] == 403
    assert _post(srv, good, Origin="http://evil.test")[0] == 403
    assert _post(srv, good, Origin=None)[0] == 403
    assert _post(srv, good, Host="example.test")[0] == 403
    assert len(calls) == 1  # nothing refused ran a program


def test_the_open_call_refuses_what_the_page_did_not_offer(served):
    srv, calls = served
    assert _post(srv, {"opener": "terminal", "kind": "session", "id": "nope"})[0] == 404
    assert _post(srv, {"opener": "browser", "kind": "session", "id": SID})[0] == 409
    assert _post(srv, {"opener": "terminal", "kind": "session"})[0] == 400
    assert _post(srv, {"opener": ["terminal"], "kind": "session", "id": SID})[0] == 400
    assert _post(srv, {"opener": "terminal", "kind": "session", "id": "x" * 300})[0] == 400
    assert calls == []


def test_the_server_binds_loopback_only(served):
    srv, _ = served
    assert srv.httpd.server_address[0] == "127.0.0.1"
