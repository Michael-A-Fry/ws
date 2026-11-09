"""A message sent to a session that is busy reaches the model at its next turn, as untrusted data, on both runners: a
real Claude Code and a real OpenCode, each driven by a scripted stub model (no network, no real key). The session is kept
busy by a shell call that sleeps; the message is sent from another registered session while it sleeps, and the
stub's next request has to carry it.

A missing runner skips the test, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import threading
import time

from e2e import test_gate_runners as gr
from e2e.msg_support import receiver, requests_text, send_from_another_session, setup
from e2e.stubs import AnthropicStub, Call, OpenAIStub

ws_home = gr.ws_home
work = gr.work

BODY = "please re-run the lint step and tell me what fails"
SLEEP = Call("Bash", {"command": "sleep 6", "description": "busy"})
OC_SLEEP = Call("bash", {"command": "sleep 6", "description": "busy"})


def sender(tmp_path, monkeypatch, done: threading.Event, stub):
    def run():
        sid = receiver(tmp_path)
        while not stub.requests:  # the model has answered with the sleeping call: the session is busy now
            time.sleep(0.1)
        time.sleep(2)
        send_from_another_session(monkeypatch, sid, BODY)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def carried(stub) -> str:
    """The text of the first request that holds the message, or "" when none does. The first request is made before the
    message is sent, so it must not."""
    texts = requests_text(stub)
    assert BODY not in texts[0]
    return next((text for text in texts[1:] if BODY in text), "")


def test_claude_code_gets_a_message_sent_while_it_is_busy(tmp_path, work, monkeypatch):
    out, env = setup(tmp_path, "claude-code", monkeypatch)
    done = threading.Event()
    stub = AnthropicStub([SLEEP])
    with stub:
        t = sender(tmp_path, monkeypatch, done, stub)
        proc = gr.claude_run(tmp_path, stub, out / "settings.json", False, work, extra_env=env)
        t.join(timeout=5)
    assert proc.returncode == 0, gr.where(proc)
    assert done.is_set()
    text = carried(stub)
    assert text, "the model never saw the message"
    assert "untrusted" in text.lower() and "grants no permission" in text


def test_opencode_gets_a_message_sent_while_it_is_busy(tmp_path, work, monkeypatch):
    out, env = setup(tmp_path, "opencode", monkeypatch)
    done = threading.Event()
    with OpenAIStub([OC_SLEEP]) as stub:
        t = sender(tmp_path, monkeypatch, done, stub)
        proc = gr.opencode_run(tmp_path, stub, out, work, extra_env=env)
        t.join(timeout=5)
    assert proc.returncode == 0, gr.where(proc)
    assert done.is_set()
    text = carried(stub)
    assert text, "the model never saw the message"
    assert "untrusted" in text.lower() and "grants no permission" in text


def test_a_message_asking_for_a_denied_action_is_still_denied_on_opencode(tmp_path, work, monkeypatch):
    """The message asks for a read the policy denies; the model, as scripted, tries it next; the gate still says no."""
    out, env = setup(tmp_path, "opencode", monkeypatch)
    canary = tmp_path / "home" / ".ssh" / "id_rsa"
    canary.parent.mkdir(parents=True)
    canary.write_text("canary-" + gr.os.urandom(6).hex())
    ask = Call("bash", {"command": f"cat {canary}", "description": "as asked"})
    with OpenAIStub([OC_SLEEP, ask]) as stub:
        t = sender(tmp_path, monkeypatch, threading.Event(), stub)
        proc = gr.opencode_run(tmp_path, stub, out, work, extra_env=env)
        t.join(timeout=5)
    assert proc.returncode == 0, gr.where(proc)
    assert canary.read_text() not in "".join(stub.tool_results())
