"""The box's four pickers and the call that starts a session: choices come from real data, a model a workspace's class
rules out is refused as well as greyed, and the profile's `[home.start] command` runs with no shell."""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import replace

import pytest

from home import SID, SID2, Box
from ws.home import api, hub, page
from ws.home.server import HomeServer

START = '[home.start]\ncommand = ["starter", "{what}", "{workspace}", "{where}", "{runner}", "{model}", "{request}"]\n'
RULES = (
    '[home.workspaces]\napp = "financial"\n[home.models]\nclaude-code = ["opus", "sonnet"]\nopencode = ["qwen"]\n'
    '[home.deny]\nopencode = ["financial"]\n"claude-code/sonnet" = ["segregated"]\n'
)


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    b.profile(START + RULES)
    b.ws_session(SID, cwd=str(b.home / "code" / "app"))
    return b


def env_of(box, calls, code=0, error=None):
    def run(argv, timeout_s):
        if argv[0] in ("claude", "tmux"):
            return 1, ""
        calls.append(argv)
        if error:
            raise error
        return code, ""

    return box.env(run=run)


GOOD = {"what": "session", "workspace": "app", "where": "testhost", "runner": "claude-code", "model": "opus",
        "request": "check the rebalance"}  # fmt: skip


def test_the_pickers_are_filled_from_the_records_and_the_profile(box):
    box.ws_session(SID2, cwd=str(box.home / "code" / "other"))
    env = env_of(box, [])
    h = hub.build(api.snapshot(env), env)
    assert {w["name"]: w["cls"] for w in h.workspaces} == {"app": "financial", "other": ""}
    assert h.machines == ("testhost",) and h.start
    runners = {r["name"]: r for r in h.runners}
    assert {"claude-code", "opencode"} <= set(runners)
    assert [m["name"] for m in runners["claude-code"]["models"]] == ["opus", "sonnet"]
    assert runners["opencode"]["models"][0]["deny"] == ("financial",)
    sonnet = runners["claude-code"]["models"][1]
    assert sonnet["deny"] == ("segregated",)


def test_a_runner_the_profile_names_no_models_for_offers_its_own_default(box):
    box.profile(START)
    env = env_of(box, [])
    h = hub.build(api.snapshot(env), env)
    assert all(r["models"] == ({"name": "", "deny": ()},) for r in h.runners)


def test_a_folder_only_finished_jobs_ran_in_is_not_a_workspace(box):
    env = env_of(box, [])
    snap = api.snapshot(env)
    ended = [replace(s, state="ended") for s in snap.sessions]
    h = hub.build(replace(snap, sessions=tuple(ended)), env)
    assert [w["name"] for w in h.workspaces] == ["app"]  # still named: the profile lists it


def test_a_workspace_only_opaque_sessions_were_seen_in_is_terminal_only(box):
    box.profile(START + '[home]\nopaque_paths = ["' + str(box.home / "code" / "app") + '"]\n')
    env = env_of(box, [])
    h = hub.build(api.snapshot(env), env)
    assert [(w["name"], w["cls"]) for w in h.workspaces] == [("app", hub.OPAQUE_CLASS)]


def test_start_runs_the_template_with_no_shell(box):
    calls: list = []
    code, body = hub.start(env_of(box, calls), {**GOOD, "request": "do it; rm -rf / $(x)"})
    assert code == 200 and body == {"ok": True}
    assert calls == [["starter", "session", "app", "testhost", "claude-code", "opus", "do it; rm -rf / $(x)"]]


@pytest.mark.parametrize(
    "change",
    [
        {"what": "daemon"},
        {"workspace": "elsewhere"},
        {"where": "another"},
        {"runner": "nothing"},
        {"model": "gpt"},
        {"runner": "opencode", "model": "qwen"},  # financial data can't go to opencode (your [home.deny])
        {"request": "-rf"},
    ],
)
def test_start_refuses_a_choice_that_is_not_on_offer_or_a_class_rules_out(box, change):
    calls: list = []
    code, body = hub.start(env_of(box, calls), {**GOOD, **change})
    assert code in (400, 409) and "error" in body and calls == []


def test_start_with_nothing_to_say_or_no_template_is_refused(box):
    calls: list = []
    assert hub.start(env_of(box, calls), {**GOOD, "request": "  "})[0] == 400
    box.profile(RULES)
    code, body = hub.start(env_of(box, calls), GOOD)
    assert code == 409 and "[home.start] command" in body["error"] and calls == []


def test_start_says_when_the_program_stops_or_is_not_there(box):
    assert hub.start(env_of(box, [], code=2), GOOD)[0] == 502
    assert hub.start(env_of(box, [], error=FileNotFoundError("starter")), GOOD)[0] == 502


def test_the_box_greys_out_a_model_the_workspace_class_rules_out_and_says_why(box):
    env = env_of(box, [])
    snap = api.snapshot(env)
    out = page.render(snap, live=True, hub=hub.build(snap, env))
    assert "disabled" not in out.split('id="c-send"')[1].split(">")[0]
    qwen = out.split('value="opencode|qwen"')[1].split("</option>")[0]
    assert " disabled" in qwen and "not for financial data (your policy)" in qwen


def test_the_send_button_is_off_and_says_why_without_a_template_or_a_server(box):
    env = env_of(box, [])
    snap = api.snapshot(env)
    served = page.render(snap, live=True, hub=hub.build(snap, env))
    assert "Sessions start from the page that ws home serves" not in served
    box.profile(RULES)
    env = env_of(box, [])
    snap = api.snapshot(env)
    no_template = page.render(snap, live=True, hub=hub.build(snap, env))
    assert "Set [home.start] command to start sessions from here" in no_template and "disabled" in no_template
    static = page.render(snap, live=False, hub=hub.build(snap, env))
    assert "Sessions start from the page that ws home serves" not in static


def test_the_note_under_the_box_is_empty_when_a_start_command_is_set(box):
    box.profile(RULES + '[home.start]\ncommand = ["ws", "dispatch", "{workspace}", "{request}"]\n')
    env = env_of(box, [])
    snap = api.snapshot(env)
    out = page.render(snap, live=True, hub=hub.build(snap, env))
    assert '<p class="cnote" id="c-note" role="status"></p>' in out
    assert "start sessions from here" not in out


# --- the server ------------------------------------------------------------------------------------------------------


def _post(srv, body, **kw):
    h = {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": f"http://127.0.0.1:{srv.port}",
         "Host": f"127.0.0.1:{srv.port}"}  # fmt: skip
    h.update(kw)
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.request("POST", "/api/start", json.dumps(body), {k: v for k, v in h.items() if v is not None})
        return conn.getresponse().status
    finally:
        conn.close()


def test_the_start_call_needs_the_token_origin_and_host(box):
    calls: list = []
    srv = HomeServer(lambda: env_of(box, calls), idle_seconds=30)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    try:
        assert _post(srv, GOOD, **{"X-Ws-Token": None}) == 403
        assert _post(srv, GOOD, **{"X-Ws-Token": "wrong"}) == 403
        assert _post(srv, GOOD, Origin="http://evil.test") == 403
        assert _post(srv, GOOD, Host="example.test") == 403
        assert calls == []
        assert _post(srv, {**GOOD, "request": ["x"]}) == 400
        assert _post(srv, {**GOOD, "model": "gpt"}) == 409 and calls == []
        assert _post(srv, GOOD) == 200 and len(calls) == 1
    finally:
        srv.close()
        t.join(5)
