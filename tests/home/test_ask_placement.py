"""An ask that names a tmux target and no session id is placed by the live Claude Code session in that pane or window:
that session's folder decides whether the ask is shown. No match, or more than one, leaves it opaque."""

from __future__ import annotations

import json

import pytest

from home import SID, SID2, Box, agent, iso
from ws.home import api


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def put_ask(box, ident="a20261102T120000-aaa1", tmux="ws:@1", name="", redacted=False):
    session = {"tmux": tmux, **({"name": name} if name else {})}
    line = {"event": "added", "id": ident, "ts": iso(-30), "repo": "app", "question": "Which one?", "session": session,
            "redacted": redacted}  # fmt: skip
    path = box.usage / "asks" / "asks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def live(box, sid, tmux, cwd=None, name="builder"):
    cwd = cwd or str(box.home / "code" / "app")
    box.pid_file(1000 + int(sid[-1]), sid, tmux=tmux)
    return agent(sid=sid, cwd=cwd, name=name)


def run_with(windows="", agents=()):
    def run(argv, timeout_s):
        if argv[0] == "tmux":
            assert argv[:3] == ["tmux", "list-windows", "-a"]
            return (0, windows) if windows is not None else (1, "")
        return 0, json.dumps(list(agents))

    return run


def asks_of(box, agents, windows=""):
    return api.asks(box.env(run=run_with(windows, agents)))


def test_an_ask_in_the_window_of_an_open_live_session_is_shown(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:@1")
    (ask,) = asks_of(box, [a])
    assert not ask.opaque and ask.title == "Which one?" and ask.repo == "app"


def test_an_ask_in_the_pane_of_an_open_live_session_is_shown(box):
    a, b = live(box, SID, "ws:@1.%1"), live(box, SID2, "ws:@1.%2")
    put_ask(box, tmux="ws:@1.%2")  # two sessions share the window; the pane tells them apart
    (ask,) = asks_of(box, [a, b])
    assert not ask.opaque and ask.title == "Which one?"


def test_a_window_number_is_found_through_tmux(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:2", name="builder")  # a number moves, so both sides must name the session
    (ask,) = asks_of(box, [a], windows="ws:1 @0\nws:2 @1\n")
    assert not ask.opaque
    put_ask(box, ident="a20261102T120000-aaa2", tmux="ws:2")  # no name on the ask: the number alone isn't enough
    assert sorted(x.opaque for x in asks_of(box, [a], windows="ws:1 @0\nws:2 @1\n")) == [False, True]


def test_a_window_number_with_no_tmux_to_ask_stays_opaque(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:2")
    (ask,) = asks_of(box, [a], windows=None)
    assert ask.opaque and ask.title == ""


def test_an_ask_in_the_window_of_a_session_under_opaque_paths_is_opaque(box):
    private = box.home / "code" / "private"
    box.profile(f'[home]\nopaque_paths = ["{private}"]\n')
    a = live(box, SID, "ws:@1.%1", cwd=str(private / "case"))
    put_ask(box, tmux="ws:@1")
    (ask,) = asks_of(box, [a])
    assert ask.opaque and ask.title == "" and ask.repo == "app" and ask.tmux == "ws:@1"


def test_an_ask_with_no_matching_session_is_opaque(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:@9")
    put_ask(box, ident="a20261102T120000-aaa2", tmux="other:@1")  # a different tmux session
    asks = asks_of(box, [a])
    assert len(asks) == 2 and all(x.opaque for x in asks)


def test_an_ask_with_no_tmux_target_is_opaque(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="")
    assert all(x.opaque for x in asks_of(box, [a]))


def test_a_tmux_target_that_matches_two_sessions_is_opaque(box):
    a, b = live(box, SID, "ws:@1.%1"), live(box, SID2, "ws:@1.%2")
    put_ask(box, tmux="ws:@1")  # the window holds two sessions: which one asked is unknown
    put_ask(box, ident="a20261102T120000-aaa2", tmux="ws:@1.%1")
    ask1, ask2 = sorted(asks_of(box, [a, b]), key=lambda x: x.id)
    assert ask1.opaque and not ask2.opaque  # the pane form is exact, the window form is not


def test_an_ended_session_does_not_place_an_ask(box):
    live(box, SID, "ws:@1.%1")  # its files are there, but the live list no longer names it
    put_ask(box, tmux="ws:@1")
    assert asks_of(box, [])[0].opaque


def test_the_ask_and_the_session_must_agree_on_the_name_when_both_have_one(box):
    a = live(box, SID, "ws:@1.%1", name="builder")
    put_ask(box, tmux="ws:@1", name="builder")
    put_ask(
        box, ident="a20261102T120000-aaa2", tmux="ws:@1", name="someone else"
    )  # the window number may have moved since
    ask1, ask2 = sorted(asks_of(box, [a]), key=lambda x: x.id)
    assert not ask1.opaque and ask2.opaque


def test_an_ask_the_source_marks_redacted_stays_opaque_even_in_an_open_window(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:@1", redacted=True)
    assert asks_of(box, [a])[0].opaque


def test_a_session_only_the_ws_files_know_cannot_vouch(box):
    box.ws_session(SID, cwd=str(box.home / "code" / "app"))
    box.pid_file(1001, SID, tmux="ws:@1.%1")
    put_ask(box, tmux="ws:@1")
    assert asks_of(box, [])[0].opaque  # claude_code didn't list it, so it saw no folder


def test_the_snapshot_places_asks_the_same_way(box):
    a = live(box, SID, "ws:@1.%1")
    put_ask(box, tmux="ws:@1")
    snap = api.snapshot(box.env(run=run_with("", [a])))
    assert [x.opaque for x in snap.asks] == [False]
