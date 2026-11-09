"""Waking an idle session: `ws msg wait --session-hook` (what a Claude Code asyncRewake hook runs), the one-waiter lock,
a waiter that outlives its session, and the tmux fallback for a runner with no hook of its own."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_msg import World, world  # noqa: F401 - the fixture, imported so this file uses the same registry stand-ins

from ws import cli, msg, session
from ws.output import WsError

INBOX_ON = '[guards]\non = ["inbox"]\n'
SCRIPT = textwrap.dedent(
    """
    import json, os, sys
    from ws import cli, msg, session
    env = json.loads(sys.argv[1])
    session.trusted_env = lambda: env  # the test's folders, as the fixture has them in this process
    msg.WAIT_POLL_S = 0.1
    open(sys.argv[2], "w").write(str(os.getpid()))
    raise SystemExit(cli.main(["msg", "wait", "--session-hook", *sys.argv[3:]]))
    """
)


def waiter(world, name: str, *args: str):  # noqa: F811
    """`ws msg wait --session-hook` as a real child process (its pid goes in `<name>.pid`), stderr and stdout piped."""
    pidfile = world.root / f"{name}.pid"
    folders = json.dumps(session.trusted_env())
    proc = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, folders, str(pidfile), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return proc


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")  # a zombie is not running


def finish(proc: subprocess.Popen, timeout: float = 20):
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


@pytest.fixture
def pair(world):  # noqa: F811
    """alpha sends; bravo's runner is this test process (so a child of the test is a child of bravo's runner)."""
    world.policy(INBOX_ON)
    me = session.proc_info(os.getpid())
    world.session("alpha", world.folder("alpha"))
    world.session("bravo", world.folder("bravo"), proc=me)
    return world


def test_wait_exits_2_with_an_envelope_only_when_a_message_lands(pair):
    proc = waiter(pair, "w", "--timeout", "30")
    time.sleep(0.4)
    assert proc.poll() is None  # nothing yet: it waits
    secret = "words that must never ride the wake line"
    sent = pair.send("alpha", "bravo", "ask", secret)
    code, out, err = finish(proc)
    assert code == 2 and out == ""  # asyncRewake: exit 2 wakes the session with what was printed
    assert f"#{sent['id']} from alpha (ask, S)" in err and "1 new message" in err
    assert secret not in err and "sha" not in err.lower()  # the envelope, never a body or a hash
    assert err.count("\n") == 1  # one line
    # a message that a hook has already delivered does not wake anyone
    msg.mark_delivered(sent["id"], "bravo")
    code, out, err = finish(waiter(pair, "w2", "--timeout", "1"))
    assert (code, out, err) == (0, "", "")
    # a message already waiting wakes at once, once: the same ids are not woken for again
    later = pair.send("alpha", "bravo", "note", "another")
    first = finish(waiter(pair, "w3", "--timeout", "30"))
    assert first[0] == 2 and f"#{later['id']}" in first[2]
    again = finish(waiter(pair, "w4", "--timeout", "1"))
    assert again == (0, "", "")


def test_wait_wakes_for_a_walled_receivers_message_with_the_envelope_only(world):  # noqa: F811
    from test_msg import classed

    classed(world)
    world.policy(world.root.joinpath("wshome", "policy.toml").read_text() + INBOX_ON)
    me = session.proc_info(os.getpid())
    world.session("wall2", world.folder("walled/two"), proc=me)
    world.send("lead1", "wall2", "note", "a walled body")
    code, _, err = finish(waiter(world, "ww", "--timeout", "30"))
    assert code == 2 and "a walled body" not in err and "from lead1 (note" in err


def test_wait_leaves_no_process_after_the_session_ends(pair):
    """The session's runner is a shell that starts the waiter; when the runner goes, the waiter goes and frees its lock."""
    go, pidfile = pair.root / "go", pair.root / "left.pid"
    folders = json.dumps(session.trusted_env())
    script = 'while [ ! -f "$1" ]; do sleep 0.05; done; "$0" -c "$2" "$3" "$4" --timeout 600 & wait'
    runner = subprocess.Popen(["sh", "-c", script, sys.executable, str(go), SCRIPT, folders, str(pidfile)])
    shell = session.proc_info(runner.pid)
    stamp = session.Proc(shell.pid, shell.start, shell.ppid, "runner")  # a shell is skipped as a runner: name it
    pair.session("carol", pair.folder("carol"), proc=stamp)  # the stamp the waiter finds
    go.write_text("go")
    deadline = time.monotonic() + 15
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    assert alive(pid)  # waiting for carol's mail
    runner.kill()  # the session ends
    runner.wait()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(pid)  # nothing is left running
    with msg._one_waiter("carol") as sole:  # and the lock it held (carol's, not another session's) is free again
        assert sole is True


def test_one_waiter_per_session(pair):
    first = waiter(pair, "a", "--timeout", "30")
    time.sleep(0.4)
    assert first.poll() is None
    second = waiter(pair, "b", "--timeout", "30")
    assert finish(second, 10) == (0, "", "")  # a second waiter for the same session leaves at once
    assert first.poll() is None  # and the first is untouched
    first.kill()  # the hook is killed: no lock is left behind
    first.communicate()
    third = waiter(pair, "c", "--timeout", "30")
    time.sleep(0.5)
    assert third.poll() is None  # it holds the lock now
    third.kill()
    third.communicate()


def test_wait_with_the_inbox_off_exits_at_once(world):  # noqa: F811
    world.policy('[guards]\non = ["syntax-check"]\n')
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = "bravo"
    began = time.monotonic()
    assert cli.main(["msg", "wait", "--session-hook"]) == 0
    assert time.monotonic() - began < 2


def test_a_hook_that_cant_tell_who_it_is_fails_silent_and_a_person_gets_the_error(world, capsys):  # noqa: F811
    world.policy(INBOX_ON)
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = None
    assert cli.main(["msg", "wait", "--session-hook", "--timeout", "1"]) == 0
    assert capsys.readouterr() == ("", "")
    assert cli.main(["msg", "wait", "--timeout", "1", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "refused"


def test_wait_is_bounded_and_the_hook_must_allow_longer(world, capsys):  # noqa: F811
    assert msg.WAIT_MAX_S < 3600 and msg.WAIT_MAX_S >= 600
    from ws.adapters import claude_code

    assert msg.WAIT_MAX_S > claude_code.HOOK_TIMEOUT  # a plain hook gets 30 s: the waiter's hook is given a longer one
    assert msg.HOOK_WAIT_MAX_S >= 6 * 3600  # an idle session is woken for hours after its last turn
    assert msg.HOOK_WAIT_MAX_S < claude_code.WAKE_TIMEOUT  # and the waiter ends itself before the runner kills it
    world.policy(INBOX_ON)
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = "bravo"
    began = time.monotonic()
    assert cli.main(["msg", "wait", "--timeout", "0.3", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data == {"session": "bravo", "arrived": False, "messages": []} and time.monotonic() - began < 3
    # the hook command that runs the waiter, in generated settings, gets a timeout that outlasts it
    from ws import paths, policy
    from ws.adapters import claude_code as cc

    on = policy.loads(INBOX_ON)
    text = cc.generate(on, ["ws"], Path("/x"), Path("/x/policy.toml"), pin=paths.pin_of())[cc.FILE]
    seen = [
        hook
        for groups in json.loads(text)["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if "msg wait --session-hook" in hook["command"]
    ]
    assert len(seen) == 1  # the loop below must have something to look at
    assert seen[0]["timeout"] > msg.HOOK_WAIT_MAX_S


def test_a_hook_waiter_exits_2_only_for_a_delivered_message(world, monkeypatch, capsys):  # noqa: F811
    """A hook's exit 2 wakes the session, so every outcome that is not a message is 0 and prints nothing."""
    world.policy(INBOX_ON)
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = "bravo"
    hook = ["msg", "wait", "--session-hook", "--timeout", "0.3"]
    assert cli.main(hook) == 0 and capsys.readouterr() == ("", "")  # nothing arrived: it gave up
    world.who = None
    assert cli.main(hook) == 0 and capsys.readouterr() == ("", "")  # no caller: a usage-kind WsError inside
    world.who = "bravo"
    monkeypatch.setattr(msg, "wait", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    assert cli.main(hook) == 0 and capsys.readouterr() == ("", "")  # a store or other error: not a wake
    monkeypatch.setattr(msg, "wait", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("anything")))
    assert cli.main(hook) == 0 and capsys.readouterr() == ("", "")
    monkeypatch.setattr(msg, "wait", lambda *a, **kw: [{"id": 7, "from": "alpha", "kind": "note", "size": "S"}])
    assert cli.main(hook) == 2  # a delivered message is the one 2
    assert "#7 from alpha" in capsys.readouterr().err


def test_a_hook_waiter_does_not_hold_a_one_shot_headless_run_open(world, monkeypatch, capsys):  # noqa: F811
    world.policy(INBOX_ON)
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = "bravo"
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")  # what `claude -p` sets for its hooks
    monkeypatch.setattr(session, "proc_args", lambda pid: "claude -p go")
    monkeypatch.setattr(msg, "wait", lambda *a, **kw: pytest.fail("a headless run has no idle to wake"))
    began = time.monotonic()
    assert cli.main(["msg", "wait", "--session-hook"]) == 0 and time.monotonic() - began < 2
    assert capsys.readouterr() == ("", "")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")  # an interactive session still waits
    ran = []
    monkeypatch.setattr(msg, "wait", lambda *a, **kw: ran.append("cli"))
    assert cli.main(["msg", "wait", "--session-hook"]) == 0 and ran == ["cli"]
    # a headless session fed by stream-json stays up between turns, so it is woken like any other
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    monkeypatch.setattr(session, "proc_args", lambda pid: "claude -p --input-format stream-json --verbose")
    assert cli.main(["msg", "wait", "--session-hook"]) == 0 and ran == ["cli", "cli"]
    monkeypatch.setattr(session, "proc_args", lambda pid: "claude -p --input-format=text go")
    assert cli.main(["msg", "wait", "--session-hook"]) == 0 and ran == ["cli", "cli"]  # one-shot: no waiter
    monkeypatch.setattr(session, "proc_args", lambda pid: None)  # unreadable: counted as one-shot
    assert cli.main(["msg", "wait", "--session-hook"]) == 0 and ran == ["cli", "cli"]


def test_proc_args_reads_a_running_process_and_none_for_a_gone_one():
    mine = session.proc_args(os.getpid())
    assert mine is not None and ("pytest" in mine or "python" in mine)
    assert session.proc_args(2**22 + 12345) is None


def test_a_hook_waiter_is_given_the_long_ceiling_and_a_person_the_short_one(world, monkeypatch):  # noqa: F811
    world.policy(INBOX_ON)
    world.session("bravo", world.folder("bravo"), proc=session.proc_info(os.getpid()))
    world.who = "bravo"
    got = []
    monkeypatch.setattr(msg, "wait", lambda sid, **kw: got.append(kw["timeout_s"]))
    assert cli.main(["msg", "wait", "--session-hook"]) == 0
    assert cli.main(["msg", "wait", "--session-hook", "--timeout", "999999"]) == 0
    assert cli.main(["msg", "wait", "--timeout", "999999"]) == 0
    assert got == [msg.HOOK_WAIT_MAX_S, msg.HOOK_WAIT_MAX_S, msg.WAIT_MAX_S]


# --- the tmux fallback ----------------------------------------------------------------------------------------------


class FakeTmux:
    """Stands in for `tmux`: records every call and its stdin, answers the pane query with a process id."""

    def __init__(self, pane_pid: int, fail: str | None = None):
        self.pane_pid, self.fail, self.calls, self.log = pane_pid, fail, [], []

    def run(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        self.log.append(("tmux", args[0]))
        out = f"{self.pane_pid}\n" if args[0] == "display-message" else ""
        return subprocess.CompletedProcess(args, 1 if args[0] == self.fail else 0, out, "")

    def sleep(self, seconds):
        self.log.append(("sleep", seconds))


def target(pane="%7", runner_pid=None) -> session.Record:
    me = session.proc_info(os.getpid())
    return session.Record(
        session_id="rx", release="1", runner="codex", pane=pane, runner_pid=runner_pid or me.pid, runner_start=me.start
    )


def test_tmux_fallback_sends_text_then_enter_and_reports_a_turn_that_did_not_start():
    fake = FakeTmux(os.getpid())
    # long text goes by paste buffer, the Enter is its own call after a pause, and a turn that never starts is reported
    got = msg.wake_pane(target(), run=fake.run, sleep=fake.sleep, started=lambda: False)
    assert got == {"woke": False, "reason": "turn_not_started", "enters": 2}
    verbs = [c[0][0] for c in fake.calls]
    assert verbs == ["display-message", "load-buffer", "paste-buffer", "send-keys", "send-keys"]
    assert fake.calls[1][1] == msg.WAKE_TEXT and "-" in fake.calls[1][0]  # the text goes by stdin to a named buffer
    paste = fake.calls[2][0]
    assert paste[:3] == ["paste-buffer", "-d", "-p"] and ["-t", "%7"] == paste[-2:]
    assert fake.calls[3][0] == ["send-keys", "-t", "%7", "Enter"] == fake.calls[4][0]
    assert all("Enter" not in " ".join(c[0]) for c in fake.calls[:3])  # never text and Enter together
    order = fake.log
    assert order.index(("sleep", msg.WAKE_KEY_DELAY_S)) < order.index(("tmux", "send-keys"))  # a pause before Enter
    # started after the first Enter: one Enter, woke
    fake = FakeTmux(os.getpid())
    answers = iter([True])
    got = msg.wake_pane(target(), run=fake.run, sleep=fake.sleep, started=lambda: next(answers, True))
    assert got == {"woke": True, "reason": "started", "enters": 1}
    # started only after the second: the missing Enter was sent again, once
    fake = FakeTmux(os.getpid())
    seen = []
    got = msg.wake_pane(target(), run=fake.run, sleep=fake.sleep, started=lambda: (seen.append(1), len(seen) > 12)[1])
    assert got["woke"] is True and got["enters"] == 2


def test_short_text_goes_by_send_keys_and_a_failed_send_is_reported():
    fake = FakeTmux(os.getpid())
    got = msg.wake_pane(target(), text="wake up", run=fake.run, sleep=fake.sleep, started=lambda: True)
    assert got["woke"] is True
    assert fake.calls[1][0] == ["send-keys", "-t", "%7", "-l", "--", "wake up"]
    assert msg.WAKE_PASTE_ABOVE == 16
    fake = FakeTmux(os.getpid(), fail="load-buffer")
    got = msg.wake_pane(target(), run=fake.run, sleep=fake.sleep, started=lambda: True)
    assert got == {"woke": False, "reason": "send_failed", "enters": 0}
    assert ["send-keys"] not in [c[0][:1] for c in fake.calls]  # nothing is typed into a pane the text didn't reach


def test_nothing_is_typed_into_a_pane_the_receiver_is_not_in():
    other = FakeTmux(os.getpid() + 100000)  # the pane's process is not an ancestor of the receiver's runner
    got = msg.wake_pane(target(), run=other.run, sleep=other.sleep, started=lambda: True)
    assert got == {"woke": False, "reason": "pane_mismatch", "enters": 0} and len(other.calls) == 1
    gone = FakeTmux(os.getpid(), fail="display-message")
    got = msg.wake_pane(target(), run=gone.run, sleep=gone.sleep, started=lambda: True)
    assert got["woke"] is False and len(gone.calls) == 1
    for rec in (target(pane=None), session.Record(session_id="x", release="1", pane="%1")):
        assert msg.wake_pane(rec, run=gone.run)["reason"] == "no_pane"


def test_the_default_confirmation_reads_the_registries_busy_state(world):  # noqa: F811
    world.policy(INBOX_ON)
    me = session.proc_info(os.getpid())
    world.session("rx", world.folder("rx"), proc=me)
    rec = session.read("rx")
    rec = session.Record(**{**rec.__dict__, "pane": "%7", "runner": "codex"})
    fake = FakeTmux(os.getpid())

    def goes_busy(seconds):
        fake.log.append(("sleep", seconds))
        if seconds == msg.WAKE_KEY_DELAY_S:
            session.update("rx", state="busy")  # the runner's own hook reports the turn

    assert msg.wake_pane(rec, run=fake.run, sleep=goes_busy)["woke"] is True
    session.update("rx", state="idle")
    got = msg.wake_pane(rec, run=fake.run, sleep=lambda s: None)
    assert got["reason"] == "turn_not_started"  # idle still: not trusted to have started


def test_send_wakes_an_idle_receiver_that_has_no_hook_of_its_own_and_reports_a_failure(world, monkeypatch, capsys):  # noqa: F811
    world.policy(INBOX_ON)
    world.session("alpha", world.folder("alpha"))
    world.session("rx", world.folder("rx"), proc=session.proc_info(os.getpid()))
    world.session("cc", world.folder("cc"))
    for sid, runner in (("rx", "codex"), ("cc", "claude-code")):
        path = world.root / "wshome" / "sessions" / f"{sid}.json"
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "pane": "%7", "runner": runner}))
        session.update(sid, state="idle")
    woken = []
    monkeypatch.setattr(
        msg,
        "wake_pane",
        lambda rec, **kw: woken.append(rec.session_id) or {"woke": False, "reason": "turn_not_started", "enters": 2},
    )
    world.who = "alpha"
    assert cli.main(["msg", "send", "rx", "hello"]) == 0
    assert cli.main(["msg", "send", "cc", "hello"]) == 0  # Claude Code has its own wake: no pane typing
    assert woken == ["rx"]
    assert "could not wake rx (turn_not_started)" in capsys.readouterr().err
    assert cli.main(["msg", "send", "rx", "again", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["wake"]["reason"] == "turn_not_started"
    session.update("rx", state="busy")
    assert cli.main(["msg", "send", "rx", "third"]) == 0 and woken == ["rx", "rx"]  # busy: not typed into


def test_a_wake_that_raises_is_reported_not_raised(world, monkeypatch):  # noqa: F811
    world.policy(INBOX_ON)
    world.session("rx", world.folder("rx"), proc=session.proc_info(os.getpid()))
    path = world.root / "wshome" / "sessions" / "rx.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "pane": "%7", "runner": "codex"}))
    session.update("rx", state="idle")
    calls = []

    def raising(rec, **kw):
        calls.append(rec.session_id)
        raise WsError("unavailable", "x", "y")

    monkeypatch.setattr(msg, "wake_pane", raising)
    got = msg._wake_after_send("rx")
    assert calls == ["rx"]  # the wake was tried
    assert got is not None and got["woke"] is False and got["reason"].startswith("error:WsError")
    assert msg._wake_after_send("nobody") is None  # nobody to wake: nothing to report
