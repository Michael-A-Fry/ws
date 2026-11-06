"""The pty bridge: one owner at a time, one size owner, the program started once at the first client's size."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from ws.term import bridge
from ws.term.testing import FakeChild


class Browser:
    """A client as the session sees it: records output, events and a close."""

    def __init__(self):
        self.out: list[bytes] = []
        self.events: list[dict] = []
        self.closed: tuple[int, str] | None = None

    def send_output(self, data: bytes) -> None:
        self.out.append(data)

    def send_event(self, event: dict) -> None:
        self.events.append(event)

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    @property
    def received(self) -> bytes:
        return b"".join(self.out)


def until(cond, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


def make(**kw) -> tuple[bridge.Session, list[FakeChild], list[tuple[int, int]]]:
    children: list[FakeChild] = []
    spawned: list[tuple[int, int]] = []

    def spawn(cols: int, rows: int) -> FakeChild:
        spawned.append((cols, rows))
        children.append(FakeChild())
        return children[-1]

    return bridge.Session(spawn, **kw), children, spawned


# -- start-up


def test_the_program_starts_at_the_first_clients_size_and_only_once():
    session, children, spawned = make()
    a = Browser()
    assert session.attach(a, 132, 43)
    assert spawned == [(132, 43)] and session.size == (132, 43)
    b = Browser()
    session.attach(b, 80, 24)
    assert spawned == [(132, 43)] and len(children) == 1, "a second client doesn't start a second program"


@pytest.mark.parametrize(
    "size", [(0, 24), (80, 0), (-1, 24), (1001, 24), (80, 1001), (True, 24), ("80", 24), (80.0, 24)]
)
def test_an_unusable_size_is_refused_before_anything_starts(size):
    session, _, spawned = make()
    assert session.attach(Browser(), *size) is False
    assert spawned == [] and session.client is None


def test_output_reaches_the_client_in_order():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    for chunk in (b"one", b"two", b"three"):
        children[0].emit(chunk)
    until(lambda: a.received == b"onetwothree")


# -- one owner


def test_a_second_client_takes_over_and_the_first_is_closed_with_4000():
    session, _, _ = make()
    a, b = Browser(), Browser()
    session.attach(a, 80, 24)
    session.attach(b, 100, 30)
    assert a.closed and a.closed[0] == bridge.CLOSE_REPLACED == 4000
    assert b.closed is None and session.client is b


def test_only_the_owner_can_type():
    session, children, _ = make()
    a, b = Browser(), Browser()
    session.attach(a, 80, 24)
    session.attach(b, 80, 24)
    assert session.write(a, b"from the old window") is False
    assert session.write(b, b"from the new one") is True
    assert children[0].typed == b"from the new one"


def test_only_the_owner_sets_the_size_and_a_repeat_is_ignored():
    seen: list[tuple[int, int]] = []
    session, children, _ = make(on_resize=lambda c, r: seen.append((c, r)))
    a, b = Browser(), Browser()
    session.attach(a, 80, 24)
    session.attach(b, 80, 24)
    assert session.resize(a, 200, 50) is False, "the replaced client has no say"
    assert session.resize(b, 90, 30) is True
    assert session.resize(b, 90, 30) is False, "the same size again does nothing"
    assert children[0].sizes == [(90, 30)] and seen == [(90, 30)] and session.size == (90, 30)


@pytest.mark.parametrize("size", [(0, 5), (5, 0), (2000, 5), (5, 2000), (None, 5), (5.5, 5)])
def test_a_resize_out_of_range_is_refused(size):
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    assert session.resize(a, *size) is False
    assert children[0].sizes == [] and session.size == (80, 24)


def test_a_client_that_left_can_neither_type_nor_resize():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    session.detach(a)
    assert session.write(a, b"x") is False and session.resize(a, 90, 30) is False
    assert children[0].typed == b""


# -- coming back


def test_the_program_keeps_running_when_the_client_leaves_and_the_next_one_gets_a_redraw():
    session, children, spawned = make()
    a = Browser()
    session.attach(a, 80, 24)
    session.detach(a)
    assert children[0].closed is False
    b = Browser()
    session.attach(b, 80, 24)
    assert spawned == [(80, 24)] and children[0].winches == 1, "same size: a signal is the only way to redraw"
    c = Browser()
    session.attach(c, 120, 40)
    assert children[0].sizes == [(120, 40)], "a new size is applied (and the program redraws by itself)"


def test_output_while_nobody_is_attached_is_dropped_without_error():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    session.detach(a)
    children[0].emit(b"nobody is looking")
    children[0].emit(b"still fine")
    b = Browser()
    session.attach(b, 80, 24)
    children[0].emit(b"hello")
    until(lambda: b"hello" in b.received)


def test_a_client_whose_connection_broke_is_detached_and_the_program_keeps_running():
    class Gone(Browser):
        def send_output(self, data):
            raise BrokenPipeError

    session, children, _ = make()
    a = Gone()
    session.attach(a, 80, 24)
    children[0].emit(b"x")
    until(lambda: session.client is None)
    assert children[0].closed is False


# -- the program ends


def test_the_clients_hear_the_exit_code_and_on_exit_runs():
    codes: list[int] = []
    session, children, _ = make(on_exit=codes.append)
    a = Browser()
    session.attach(a, 80, 24)
    children[0].emit(b"bye")
    children[0].finish(7)
    until(lambda: codes == [7])
    assert a.received == b"bye" and a.events == [{"type": "exit", "code": 7}]
    assert session.exit_code == 7 and session.write(a, b"x") is False


def test_a_client_that_attaches_after_the_exit_is_told_at_once():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    children[0].finish(0)
    until(lambda: session.exit_code == 0)
    b = Browser()
    assert session.attach(b, 80, 24)
    assert b.events == [{"type": "exit", "code": 0}]


def test_close_hangs_up_the_program_and_the_client():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    session.close()
    assert children[0].closed and a.closed and a.closed[0] == 1001


def test_close_before_any_client_is_harmless():
    session, _, _ = make()
    session.close()


# -- the environment a session's program gets


def test_the_environment_is_the_persons_own_as_a_terminal_outside_tmux():
    base = {
        "HOME": "/h",
        "SHELL": "/bin/zsh",
        "TERM": "dumb",
        "TMUX": "/tmp/t,1,0",
        "TMUX_PANE": "%1",
        "COLORTERM": "no",
    }
    env = bridge.child_env(base, {"X": "1", "TERM": "ignored-by-extra-order"})
    assert env["HOME"] == "/h" and env["SHELL"] == "/bin/zsh"
    assert env["COLORTERM"] == "truecolor" and "TMUX" not in env and "TMUX_PANE" not in env
    assert env["TERM"] == "ignored-by-extra-order", "the extra values are added last"
    assert bridge.child_env(base)["TERM"] == "xterm-256color"
    assert base["TMUX"], "the input isn't changed"


RUNNER_VARS = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "1"}


def test_a_coding_agents_session_variables_are_not_passed_on_and_everything_else_is():
    base = {
        **RUNNER_VARS,
        "CLAUDE_CODE_FORCE_SYNC_OUTPUT": "0",
        "OPENCODE": "1",
        "CODEX_SANDBOX": "seatbelt",
        "CLAUDE_CONFIG_DIR": "/c",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "planted-value",
        "KEEP_ME": "1",
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "en_NZ.UTF-8",
        "LC_ALL": "en_NZ.UTF-8",
        "USER": "u",
        "SHELL": "/bin/zsh",
    }
    env = bridge.child_env(base)
    assert not [k for k in env if bridge.is_runner_session_var(k)], "no runner session variable is left"
    kept = {"KEEP_ME", "PATH", "HOME", "LANG", "LC_ALL", "USER", "SHELL", "CLAUDE_CONFIG_DIR", "TERM", "COLORTERM"}
    kept |= {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_FORCE_SYNC_OUTPUT"}  # the person's own
    assert set(env) == kept
    assert all(k in base for k in RUNNER_VARS), "the input isn't changed"
    named = bridge.child_env(base, {"CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1", "CLAUDE_CODE_ENTRYPOINT": "mine"})
    assert named["CLAUDE_CODE_FORCE_SYNC_OUTPUT"] == "1" and named["CLAUDE_CODE_ENTRYPOINT"] == "mine"
    assert "CLAUDECODE" not in named and "CLAUDE_CODE_SSE_PORT" not in named, "only what is named comes back"


# -- a real pty (POSIX)

posix = pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")


def read_until(child, needle: bytes, seconds: float = 5.0) -> bytes:
    got = b""
    done = threading.Event()

    def pump():
        nonlocal got
        while needle not in got:
            data = child.read()
            if not data:
                break
            got += data
        done.set()

    threading.Thread(target=pump, daemon=True).start()
    assert done.wait(seconds), f"timed out waiting for {needle!r}; got {got!r}"
    return got


@pytest.fixture
def sane_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return bridge.child_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)})


@posix
def test_a_real_pty_has_the_size_it_was_started_with(sane_env):
    child = bridge.open_pty(["/bin/sh", "-c", "stty size"], sane_env, 101, 33)
    try:
        assert b"33 101" in read_until(child, b"33 101")
        assert child.wait() == 0
    finally:
        child.close()


@posix
def test_a_real_pty_is_resized_and_the_program_is_told(sane_env):
    script = "trap 'stty size' WINCH; echo ready; while :; do sleep 0.05; done"
    child = bridge.open_pty(["/bin/sh", "-c", script], sane_env, 80, 24)
    try:
        read_until(child, b"ready")
        child.resize(120, 40)
        child.winch()
        assert b"40 120" in read_until(child, b"40 120")
    finally:
        child.close()


@posix
def test_a_real_pty_is_a_controlling_terminal_with_the_terminal_environment(sane_env):
    child = bridge.open_pty(
        ["/bin/sh", "-c", 'tty; echo "T=$TERM C=$COLORTERM"; test -t 0 && echo istty'], sane_env, 80, 24
    )
    try:
        out = read_until(child, b"istty")
        assert b"/dev/" in out and b"T=xterm-256color C=truecolor" in out
    finally:
        child.close()


@posix
def test_a_real_pty_passes_bytes_both_ways_and_reports_the_exit_code(sane_env):
    child = bridge.open_pty(["/bin/sh", "-c", "read line; echo got:$line; exit 5"], sane_env, 80, 24)
    try:
        child.write(b"hello\n")
        assert b"got:hello" in read_until(child, b"got:hello")
        assert child.wait() == 5
    finally:
        child.close()


@posix
def test_closing_a_real_pty_stops_a_program_that_ignores_nothing(sane_env):
    child = bridge.open_pty(["/bin/sh", "-c", "echo up; while :; do sleep 0.05; done"], sane_env, 80, 24)
    read_until(child, b"up")
    child.close()
    assert child.proc.poll() is not None
    child.close()  # twice is fine


@posix
def test_closing_a_real_pty_kills_a_program_that_ignores_hangup(sane_env):
    child = bridge.open_pty(["/bin/sh", "-c", "trap '' HUP; echo up; while :; do sleep 0.05; done"], sane_env, 80, 24)
    read_until(child, b"up")
    started = time.monotonic()
    child.close()
    assert child.proc.poll() is not None and time.monotonic() - started < 6


@posix
def test_a_session_on_a_real_pty_end_to_end(sane_env):
    codes: list[int] = []
    session = bridge.Session(
        lambda cols, rows: bridge.open_pty(["/bin/cat"], sane_env, cols, rows), on_exit=codes.append
    )
    a = Browser()
    session.attach(a, 90, 25)
    session.write(a, b"ping\r")
    until(lambda: b"ping" in a.received)
    session.write(a, b"\x04")  # Ctrl-D ends cat
    until(lambda: codes == [0])
    assert a.events == [{"type": "exit", "code": 0}]
    session.close()


def test_open_pty_failure_is_an_error_and_leaks_nothing(monkeypatch, sane_env):
    if sys.platform == "win32":
        pytest.skip("needs a pty")
    import subprocess

    before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None

    def boom(*a, **k):
        raise OSError("no such thing")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(OSError):
        bridge.open_pty(["/bin/true"], sane_env, 80, 24)
    if before is not None:
        assert len(os.listdir("/dev/fd")) <= before


# -- the modes a program has set, replayed to a window that arrives later


def test_the_mode_tracker_follows_set_and_reset_and_replays_in_a_fixed_order():
    t = bridge.ModeTracker()
    t.feed(b"\x1b[?1000h\x1b[?1006h\x1b[?1049h\x1b[?25l\x1b[?2004h")
    t.feed(b"text \x1b[?1000l more")
    assert t.restore() == b"\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[?1000l\x1b[?1006h"


def test_the_mode_tracker_reads_a_sequence_split_across_chunks_at_every_byte():
    seq = b"\x1b[?1049h\x1b[?1000h"
    for cut in range(1, len(seq)):
        t = bridge.ModeTracker()
        t.feed(b"abc" + seq[:cut])
        t.feed(seq[cut:] + b"xyz")
        assert t.restore() == b"\x1b[?1049h\x1b[?1000h", cut


def test_the_mode_tracker_reads_several_modes_in_one_sequence_and_ignores_the_rest():
    t = bridge.ModeTracker()
    t.feed(b"\x1b[?1000;1006h\x1b[?9999h\x1b[1h\x1b[?x\x1b]0;title\x07\x1b[2J")
    assert t.restore() == b"\x1b[?1000h\x1b[?1006h"


def test_a_stray_escape_doesnt_hold_back_later_sequences():
    t = bridge.ModeTracker()
    t.feed(b"\x1b")
    t.feed(b"\x1b[?25l")
    t.feed(b"\x1b[31mred\x1b[?1")
    t.feed(b"049h")
    assert t.restore() == b"\x1b[?1049h\x1b[?25l"


def test_a_program_that_set_no_modes_replays_nothing():
    t = bridge.ModeTracker()
    t.feed(b"plain output\r\n")
    assert t.restore() == b""


def test_a_window_that_arrives_later_gets_the_modes_before_the_redraw():
    session, children, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    children[0].emit(b"\x1b[?1049h\x1b[2J\x1b[?1000h\x1b[?1006h\x1b[?25l> ready")
    until(lambda: b"> ready" in a.received)
    session.detach(a)
    b = Browser()
    session.attach(b, 80, 24)
    assert b.received == b"\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h"
    assert children[0].winches == 1, "the redraw signal follows the modes"
    c = Browser()
    session.attach(c, 100, 30)
    assert c.received.startswith(b"\x1b[?1049h") and children[0].sizes == [(100, 30)]


def test_the_first_window_gets_no_replay():
    session, _, _ = make()
    a = Browser()
    session.attach(a, 80, 24)
    assert a.received == b""


# -- a window that has gone without closing


def stuck_client():
    """A real `WebClient` whose connection is dead: the first write waits until the connection is cut."""
    import io

    from ws.term import app
    from ws.term import websocket as w

    entered, cut = threading.Event(), threading.Event()

    class Wire:
        def write(self, data):
            entered.set()
            cut.wait(10)
            raise BrokenPipeError("cut")

        def flush(self):
            pass

    return app.WebClient(w.WebSocket(io.BytesIO(), Wire(), abort=cut.set)), entered


def test_a_takeover_completes_while_the_old_window_is_stuck_on_a_dead_connection():
    session, children, _ = make()
    old, entered = stuck_client()
    session.attach(old, 80, 24)
    children[0].emit(b"output the dead window will never take")
    assert entered.wait(3), "the output thread is stuck writing to the old window"
    new = Browser()
    started = time.monotonic()
    assert session.attach(new, 100, 30)
    assert time.monotonic() - started < 2
    assert session.client is new and old.ws.closed
    children[0].emit(b"after")
    until(lambda: b"after" in new.received)
    assert session.write(new, b"k") and children[0].typed == b"k"
    session.close()


def test_closing_the_session_returns_while_the_window_is_stuck_on_a_dead_connection():
    session, children, _ = make()
    old, entered = stuck_client()
    session.attach(old, 80, 24)
    children[0].emit(b"output")
    assert entered.wait(3)
    started = time.monotonic()
    session.close()
    assert time.monotonic() - started < 2
    assert children[0].closed


def test_the_old_window_is_closed_outside_the_session_lock():
    session, _, _ = make()
    seen = []

    class Old(Browser):
        def close(self, code, reason):
            # another thread can take the session lock while this close runs
            t = threading.Thread(target=lambda: (session.detach(self), seen.append("free")), daemon=True)
            t.start()
            t.join(1)
            super().close(code, reason)

    old = Old()
    session.attach(old, 80, 24)
    session.attach(Browser(), 80, 24)
    assert seen == ["free"] and old.closed == (bridge.CLOSE_REPLACED, "another window took this session over")


# -- closing a real pty: stop the program, stop the reader, then close the descriptor


class FakeProc:
    pid = 4242

    def __init__(self, events):
        self.events = events

    def wait(self, timeout=None):
        self.events.append("wait")
        return 0

    def poll(self):
        return 0


def test_the_pty_is_closed_after_the_program_is_signalled_and_the_reader_has_left(monkeypatch):
    import select

    events: list[str] = []
    r, w = os.pipe()  # stands in for the pty: nothing is ever written, so a read waits
    real_close, real_select = os.close, select.select

    def killpg(pgid, sig):
        events.append("signal")

    def close(fd):
        if fd == r:
            events.append("close")
        real_close(fd)

    def select_(rl, wl, xl, *a):
        out = real_select(rl, wl, xl, *a)
        events.append("reader-woken")
        return out

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(select, "select", select_)
    child = bridge.PtyChild(FakeProc(events), r)
    got = []
    reader = threading.Thread(target=lambda: got.append(child.read()), daemon=True)
    reader.start()
    time.sleep(0.2)  # the reader is waiting in select
    child.close()
    reader.join(3)
    assert not reader.is_alive() and got == [b""]
    assert events == ["signal", "wait", "reader-woken", "close"]
    real_close(w)


def test_a_read_after_close_returns_nothing_without_touching_the_descriptor(monkeypatch):
    r, w = os.pipe()
    monkeypatch.setattr(os, "killpg", lambda *a: None)
    child = bridge.PtyChild(FakeProc([]), r)
    child.close()
    child.close()  # twice is fine
    assert child.read() == b""
    os.close(w)


@posix
def test_a_session_on_a_real_pty_closes_while_the_output_thread_is_reading(sane_env):
    session = bridge.Session(lambda cols, rows: bridge.open_pty(["/bin/sh", "-c", "sleep 30"], sane_env, cols, rows))
    session.attach(Browser(), 80, 24)
    child = session._child
    time.sleep(0.2)
    started = time.monotonic()
    session.close()
    assert time.monotonic() - started < 5
    until(lambda: not session._pump.is_alive())
    assert child.proc.poll() is not None


@posix
def test_a_real_child_started_from_inside_a_coding_agent_session_sees_none_of_its_variables(tmp_path, monkeypatch):
    for name, value in {**RUNNER_VARS, "KEEP_ME": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = bridge.child_env(os.environ, {"CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1"})
    child = bridge.open_pty(["/bin/sh", "-c", "/usr/bin/env; echo __END__"], env, 80, 24)
    try:
        out = read_until(child, b"__END__").decode()
        assert child.wait() == 0
    finally:
        child.close()
    seen = {line.split("=", 1)[0] for line in out.replace("\r", "").splitlines() if "=" in line}
    assert {"KEEP_ME", "PATH", "HOME", "TERM", "CLAUDE_CODE_FORCE_SYNC_OUTPUT"} <= seen
    assert not seen & set(RUNNER_VARS), "the program isn't told it's inside the parent's session"
    assert f"HOME={tmp_path}" in out


def test_the_tmux_server_ws_term_starts_gets_the_same_filtered_environment(monkeypatch):
    from ws.term import tmux as tmux_mod

    seen = {}
    monkeypatch.setattr(tmux_mod.subprocess, "run", lambda argv, **kw: seen.update(kw) or SimpleRun())
    env = bridge.child_env({**RUNNER_VARS, "PATH": "/bin", "KEEP_ME": "1"})
    tmux_mod.runner(env=env)(["new-session", "-d", "-s", "s"])
    assert seen["env"] == env and not set(RUNNER_VARS) & set(seen["env"])
    tmux_mod.runner()(["ls"])
    assert seen["env"] is None, "with no env given, tmux gets the process's own"


class SimpleRun:
    returncode = 0
    stdout = ""
    stderr = ""
