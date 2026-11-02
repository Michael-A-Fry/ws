"""Replay: each measured behaviour of a full-screen program, as a synthetic byte stream through the bridge.

The fixtures in tests/fixtures/term/ are written by hand to reproduce what was measured (mode sets, a repaint on every
resize, bracketed paste, key bytes, the reset on exit); none is a recording. The bridge must not change a byte.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from ws.term import bridge, cli
from ws.term.testing import FakeChild

FIX = Path(__file__).parent / "fixtures" / "term"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Browser:
    def __init__(self):
        self.out: list[bytes] = []
        self.events: list[dict] = []
        self.closed = None

    def send_output(self, data):
        self.out.append(data)

    def send_event(self, event):
        self.events.append(event)

    def close(self, code, reason):
        self.closed = (code, reason)

    @property
    def received(self) -> bytes:
        return b"".join(self.out)


def until(cond, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def session_with(child: FakeChild, **kw) -> tuple[bridge.Session, Browser]:
    s = bridge.Session(lambda c, r: child, **kw)
    b = Browser()
    s.attach(b, 100, 30)
    return s, b


def modes(stream: str) -> tuple[set[str], set[str]]:
    """The private modes left on and off after the stream, the last set or reset of each winning."""
    state: dict[str, bool] = {}
    for num, kind in re.findall(r"\x1b\[\?(\d+)([hl])", stream):
        state[num] = kind == "h"
    return {n for n, v in state.items() if v}, {n for n, v in state.items() if not v}


# -- start-up modes

STARTUP = fixture("startup_modes.json")


@pytest.mark.parametrize("size", STARTUP["chunk_sizes"])
def test_startup_bytes_arrive_unchanged_whatever_the_chunking(size):
    raw = STARTUP["stream"].encode("utf-8")
    child = FakeChild()
    _, browser = session_with(child)
    for i in range(0, len(raw), size):
        child.emit(raw[i : i + size])
    until(lambda: browser.received == raw)


def test_the_startup_stream_sets_the_modes_the_fixture_lists():
    on, off = modes(STARTUP["stream"])
    assert sorted(on) == STARTUP["modes_on"] and sorted(off) == STARTUP["modes_off"]


# -- resize: a clear and a repaint per distinct size, delivered once

RESIZE = fixture("resize_expected.json")


def test_a_repaint_per_distinct_size_and_none_for_a_repeat():
    def repaint(child: FakeChild, cols: int, rows: int) -> None:
        child.emit(RESIZE["repaint_prefix"].encode() + f"<{cols}x{rows}>".encode())

    child = FakeChild(on_resize=repaint)
    session, browser = session_with(child)
    for cols, rows in RESIZE["sizes"]:
        session.resize(browser, cols, rows)
    want = [tuple(s) for s in RESIZE["expect_delivered"]]
    until(lambda: browser.received.count(RESIZE["repaint_prefix"].encode()) == len(want))
    assert child.sizes == want, "each distinct size reached the program once"
    painted = re.findall(rb"<(\d+)x(\d+)>", browser.received)
    assert [(int(c), int(r)) for c, r in painted] == want


# -- paste

PASTE = fixture("paste_expected.json")


def test_a_ten_kilobyte_bracketed_paste_arrives_intact_in_pty_sized_reads():
    text = "\r".join(PASTE["line_template"].format(n=n) for n in range(PASTE["lines"]))
    sent = (PASTE["start_marker"] + text + PASTE["end_marker"]).encode()
    assert len(sent) > 10_000
    child = FakeChild()
    session, browser = session_with(child)
    size = PASTE["read_size"]
    for i in range(0, len(sent), size):
        assert session.write(browser, sent[i : i + size])
    assert child.typed == sent
    assert child.typed.startswith(PASTE["start_marker"].encode()) and child.typed.endswith(PASTE["end_marker"].encode())
    assert max(len(w) for w in child.written) <= size


# -- exit

EXIT = fixture("exit_flow_expected.json")


def test_the_reset_set_arrives_before_the_exit_event_and_ends_with_the_screen_restored():
    child = FakeChild()
    codes: list[int] = []
    _, browser = session_with(child, on_exit=codes.append)
    child.emit(EXIT["stream"].encode())
    child.finish(EXIT["exit_code"])
    until(lambda: codes == [EXIT["exit_code"]])
    assert browser.received == EXIT["stream"].encode()
    assert browser.events == [{"type": "exit", "code": EXIT["exit_code"]}]
    last = re.findall(r"\x1b\[(\?\d+[hl])", browser.received.decode())[-len(EXIT["ends_with_modes"]) :]
    assert last == EXIT["ends_with_modes"], "the cursor is shown, then the alternate screen is left"


# -- multiplexer passthrough and clipboard/notification escapes


@pytest.mark.parametrize("stream", fixture("passthrough_expected.json")["streams"])
def test_wrapped_and_plain_escape_strings_reach_the_browser_byte_for_byte(stream):
    child = FakeChild()
    _, browser = session_with(child)
    child.emit(stream.encode())
    until(lambda: browser.received == stream.encode())


# -- the environment that asks for synchronized output

SYNC = fixture("sync_output_gate.json")


@pytest.mark.parametrize("case", SYNC["cases"], ids=lambda c: f"tmux={c['tmux']}-off={c['no_sync_output']}")
def test_synchronized_output_is_asked_for_by_the_environment_only_outside_tmux(case, monkeypatch, tmp_path):
    import argparse

    seen = {}
    monkeypatch.setattr(cli, "open_pty", lambda argv, env, cols, rows: seen.update(argv=argv, env=env) or FakeChild())
    monkeypatch.setenv("TMUX", "/tmp/outer,1,0")  # ws term launched from inside a tmux of the person's own
    monkeypatch.setenv("SHELL", "/bin/sh")
    args = argparse.Namespace(
        bind=None,
        port=0,
        allow_host=None,
        tmux="s" if case["tmux"] else None,
        tmux_server_options=False,
        env=[],
        no_sync_output=case["no_sync_output"],
        idle_minutes=1.0,
        command=[],
        json=False,
    )
    captured = {}

    class Stub:
        url = "http://127.0.0.1:1/?t=x"
        port = 1

        def serve(self):
            captured["spawn"]()

        def stop(self):
            pass

    def fake_build(session, **kw):
        captured["spawn"] = lambda: session.attach(Browser(), 90, 25)
        return Stub()

    monkeypatch.setattr(cli, "build_server", fake_build)
    if case["tmux"]:
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)

        class T:
            skipped = ()
            adopted = False
            name = "s"

            def __init__(self, *a, **k):
                pass

            def prepare(self, cols, rows):
                return ["tmux", "attach-session"]

            def resize(self, cols, rows):
                pass

        monkeypatch.setattr(cli.tmux_mod, "Tmux", T)
    assert cli.cmd_term(args) == 0
    env = seen["env"]
    assert ("CLAUDE_CODE_FORCE_SYNC_OUTPUT" in env) is case["expect_set"]
    assert "TMUX" not in env, "the session isn't inside the tmux that launched ws term"
    assert env["TERM"] == "xterm-256color"


def test_the_startup_modes_are_replayed_to_a_window_that_connects_after_them():
    child = FakeChild()
    session, first = session_with(child)
    raw = STARTUP["stream"].encode("utf-8")
    child.emit(raw)
    until(lambda: first.received == raw)
    session.detach(first)
    late = Browser()
    session.attach(late, 100, 30)
    on, off = modes(late.received.decode())
    assert sorted(on) == STARTUP["modes_on_replayed"] and sorted(off) == STARTUP["modes_off"]
