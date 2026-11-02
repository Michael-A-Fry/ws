"""Answering an ask on its row: the API routes the answer to the source that gave the ask, which runs the profile's
`[home.answer] command` with no shell. An opaque ask, an approval and a permission are never answered this way."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from home import NOW, SID, Box, agent, iso
from ws import approvals
from ws.approvals import Bound
from ws.home import api
from ws.home.records import Ask
from ws.home.server import HomeServer
from ws.output import WsError

TEMPLATE = '[home.answer]\ncommand = ["answerer", "{id}", "{choice}", "{text}"]\n'
ASK_ID = "a20261102T120000-aaa1"
OPTIONS = [{"label": "Raise it", "description": "go on"}, {"label": "Hold", "description": ""}]


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    b.profile(TEMPLATE)
    b.pid_file(4242, SID, tmux="ws:@1.%1")
    return b


def put_ask(box, ident=ASK_ID, source="question", redacted=False, options=None):
    line = {"event": "added", "id": ident, "ts": iso(-30), "repo": "app", "question": "Which one?",
            "session": {"tmux": "ws:@1"}, "redacted": redacted, "source": source,
            "options": OPTIONS if options is None else options}  # fmt: skip
    path = box.usage / "asks" / "asks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return path


def env_of(box, calls, code=0):
    def run(argv, timeout_s):
        if argv[0] == "claude":
            return 0, json.dumps([agent(SID, cwd=str(box.home / "code" / "app"))])
        if argv[0] == "tmux":
            return 1, ""
        calls.append(argv)
        return code, ""

    return box.env(run=run)


def kind_of(e: pytest.ExceptionInfo) -> str:
    return e.value.kind


def test_a_question_on_an_open_session_is_answerable_and_the_others_are_not(box):
    put_ask(box)
    (ask,) = api.asks(env_of(box, []))
    assert api.answerable(ask)
    other = [
        Ask(id="x", source="asks_jsonl", kind="permission"),
        Ask(id="x", source="asks_jsonl", kind="question", opaque=True),
        Ask(id="x", source="ws_approvals", kind="approval"),
        Ask(id="x", source="claude_code", kind="waiting"),
        Ask(id="x", source="ws_approvals", kind="question"),  # a source that takes no answer
    ]
    assert not any(api.answerable(a) for a in other)


def test_an_option_is_sent_to_the_program_with_no_shell(box):
    put_ask(box)
    calls: list = []
    api.answer(ASK_ID, choice="Raise it", env=env_of(box, calls))
    assert calls == [["answerer", ASK_ID, "Raise it", ""]]


def test_your_own_words_are_one_argument_whatever_they_hold(box):
    put_ask(box)
    calls: list = []
    words = "hold; rm -rf / && $(x) `y`"
    api.answer(ASK_ID, text=words, env=env_of(box, calls))
    assert calls == [["answerer", ASK_ID, "", words]]


def test_the_asks_file_is_never_written_by_the_page(box):
    path = put_ask(box)
    before = path.read_bytes()
    api.answer(ASK_ID, choice="Hold", env=env_of(box, []))
    assert path.read_bytes() == before


def test_an_opaque_ask_cannot_be_answered(box):
    put_ask(box, redacted=True)
    calls: list = []
    (ask,) = api.asks(env_of(box, calls))
    assert ask.opaque
    with pytest.raises(WsError) as e:
        api.answer(ask.id, choice="Raise it", env=env_of(box, calls))
    assert kind_of(e) == "refused" and calls == []


def test_a_permission_cannot_be_answered(box):
    put_ask(box, source="permission")
    calls: list = []
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, choice="Raise it", env=env_of(box, calls))
    assert kind_of(e) == "refused" and calls == []


def test_an_approval_cannot_be_answered(box):
    root = box.ws / "approvals"
    bound = Bound("claude-code", "command", "terraform apply", str(box.home / "code" / "app"), SID)
    approvals.require(root, bound, NOW - 60)
    calls: list = []
    env = env_of(box, calls)
    (ask,) = [a for a in api.asks(env) if a.kind == "approval"]
    assert not api.answerable(ask)
    with pytest.raises(WsError) as e:
        api.answer(ask.id, choice="Yes", env=env)
    assert kind_of(e) == "refused" and calls == []


@pytest.mark.parametrize(
    ("kw", "kind"),
    [
        ({"choice": "Nope"}, "refused"),  # not one of its options
        ({"choice": "Hold", "text": "and"}, "refused"),  # both
        ({}, "refused"),  # neither
        ({"text": "x" * 2001}, "refused"),
        ({"choice": "Hold"}, "failed"),  # see below: the program stops with an error
    ],
)
def test_an_answer_that_is_not_one_the_ask_offers_is_refused(box, kw, kind):
    put_ask(box)
    calls: list = []
    code = 1 if kw == {"choice": "Hold"} else 0
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, env=env_of(box, calls, code=code), **kw)
    assert kind_of(e) == kind
    assert calls == ([["answerer", ASK_ID, "Hold", ""]] if code else [])


def test_an_ask_that_is_gone_and_no_command_set_are_their_own_errors(box):
    put_ask(box)
    with pytest.raises(WsError) as e:
        api.answer("nope", choice="Hold", env=env_of(box, []))
    assert kind_of(e) == "not_found"
    box.profile('[home]\nlead = "name:*"\n')
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, choice="Hold", env=env_of(box, []))
    assert kind_of(e) == "not_set"


def test_a_word_that_would_be_read_as_a_flag_is_refused(box):
    put_ask(box, options=[{"label": "-n", "description": ""}])
    calls: list = []
    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, choice="-n", env=env_of(box, calls))
    assert kind_of(e) == "failed" and calls == []
    with pytest.raises(WsError):
        api.answer(ASK_ID, text="-rf", env=env_of(box, calls))
    assert calls == []


def test_a_program_that_is_not_there_is_a_typed_failure(box):
    put_ask(box)

    def run(argv, timeout_s):
        if argv[0] == "claude":
            return 0, json.dumps([agent(SID, cwd=str(box.home / "code" / "app"))])
        if argv[0] == "tmux":
            return 1, ""
        raise FileNotFoundError(argv[0])

    with pytest.raises(WsError) as e:
        api.answer(ASK_ID, choice="Hold", env=box.env(run=run))
    assert kind_of(e) == "failed"


# --- the profile -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('[home.answer]\ncommand = ["a", "{id}", "{nope}"]\n', "nope"),
        ('[home.answer]\ncommand = ["a", "{choice}"]\n', "{id}"),
        ('[home.answer]\ncommand = ["{id}"]\n', "program"),
        ('[home.answer]\ncommand = "a {id}"\n', "list of words"),
        ('[home.answer]\nshell = "a"\n', "shell"),
        ('[home.start]\ncommand = ["ws", "go", "{workspace}"]\n', "{request}"),
        ("[home.workspaces]\nx = 1\n", "x"),
        ('[home.models]\nclaude-code = "opus"\n', "claude-code"),
    ],
)
def test_a_bad_command_template_is_an_error_that_names_it(box, text, named):
    box.profile(text)
    with pytest.raises(WsError) as e:
        api.Env.make(box.environ)
    assert e.value.kind == "bad_config" and named in e.value.message


def test_a_good_profile_is_read_whole(box):
    box.profile(
        '[home.start]\ncommand = ["ws", "dispatch", "{workspace}", "{request}"]\n'
        '[home.answer]\ncommand = ["ws-ask", "answer", "{id}", "{choice}"]\n'
        '[home.workspaces]\ninvestments = "financial"\n[home.models]\nclaude-code = ["opus"]\n'
        '[home.deny]\n"opencode" = ["financial"]\n'
    )
    cfg = api.Env.make(box.environ).cfg
    assert cfg.start == ("ws", "dispatch", "{workspace}", "{request}") and cfg.answer[0] == "ws-ask"
    assert cfg.workspaces == {"investments": "financial"} and cfg.models == {"claude-code": ("opus",)}
    assert cfg.deny == {"opencode": ("financial",)}


# --- the server ------------------------------------------------------------------------------------------------------


def _req(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


def _serve(box, calls):
    srv = HomeServer(lambda: env_of(box, calls), idle_seconds=30)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    return srv, t


def _post(srv, path, body, **kw):
    h = {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": f"http://127.0.0.1:{srv.port}"}
    h.update(kw)
    return _req(srv, "POST", path, json.dumps(body), {k: v for k, v in h.items() if v is not None})


def test_the_answer_call_needs_the_token_origin_and_host_and_refuses_what_it_should(box):
    put_ask(box)
    put_ask(box, ident="a20261102T120000-bbb2", redacted=True)
    calls: list = []
    srv, t = _serve(box, calls)
    try:
        good = {"id": ASK_ID, "choice": "Raise it"}
        assert _post(srv, "/api/answer", good, **{"X-Ws-Token": None})[0] == 403
        assert _post(srv, "/api/answer", good, **{"X-Ws-Token": "wrong"})[0] == 403
        assert _post(srv, "/api/answer", good, Origin="http://evil.test")[0] == 403
        assert _post(srv, "/api/answer", good, Host="example.test")[0] == 403
        assert calls == []
        assert _post(srv, "/api/answer", {"id": "x", "choice": ["a"]})[0] == 400
        assert _post(srv, "/api/answer", {"choice": "Hold"})[0] == 400
        assert _post(srv, "/api/answer", {"id": "nope", "choice": "Hold"})[0] == 404
        opaque = next(a.id for a in api.asks(env_of(box, [])) if a.opaque)
        assert _post(srv, "/api/answer", {"id": opaque, "choice": "Raise it"})[0] == 409  # opaque
        assert _post(srv, "/api/answer", {"id": ASK_ID, "choice": "Nope"})[0] == 409
        assert calls == []
        assert _post(srv, "/api/answer", good)[0] == 200
        assert calls == [["answerer", ASK_ID, "Raise it", ""]]
    finally:
        srv.close()
        t.join(5)
