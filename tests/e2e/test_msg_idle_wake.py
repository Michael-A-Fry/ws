"""A session that has finished its turn and is waiting for the person is woken by a message: the idle session starts a
turn, on a real Claude Code (an `asyncRewake` hook running `ws msg wait --session-hook`) and a real OpenCode (the
shim's waiter and `session.promptAsync`). Each runner is driven by a scripted stub model. The wake itself carries the
envelope only; the body comes to the model as untrusted data from the inbox guard.

A missing runner skips the test, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

from e2e import test_gate_runners as gr
from e2e.msg_support import receiver, requests_text, send_from_another_session, setup
from e2e.stubs import AnthropicStub, OpenAIStub
from ws import paths

ws_home = gr.ws_home
work = gr.work

BODY = "while you were idle: please check the nightly job"


def waits_for_a_new_request(stub, before: int, seconds: float = 20.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if len(stub.requests) > before:
            return True
        time.sleep(0.2)
    return False


def test_an_idle_claude_code_session_starts_a_turn_when_a_message_lands(tmp_path, work, monkeypatch):
    out, extra = setup(tmp_path, "claude-code", monkeypatch)
    settings = out / "settings.json"
    conf = json.loads(settings.read_text())
    pin = paths.pin_of({"HOME": str(tmp_path / "home"), "WS_HOME": str(tmp_path / "wshome")})
    wait = shlex.join([sys.executable, str(tmp_path / "wsx.py"), "msg", "wait", "--session-hook", *pin.args()])
    hook = {"type": "command", "command": wait, "asyncRewake": True, "timeout": 300}
    conf["hooks"].setdefault("Stop", [{"hooks": []}])[0]["hooks"].append(hook)
    settings.write_text(json.dumps(conf))
    with AnthropicStub([], final_text="all done") as stub:
        env = gr.base_env(tmp_path) | extra
        env.update(
            ANTHROPIC_BASE_URL=stub.url,
            ANTHROPIC_API_KEY="sk-ant-" + "stub" * 8,
            CLAUDE_CONFIG_DIR=str(tmp_path / "claude-config"),
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
        )
        argv = [gr.runner_bin("claude"), "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
        argv += ["--verbose", "--settings", str(settings), "--setting-sources", "user", "--no-session-persistence"]
        argv += ["--model", "claude-sonnet-4-5"]  # a session that stays up after its turn, waiting for the next input
        proc = subprocess.Popen(
            argv, env=env, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n")
            proc.stdin.flush()
            assert waits_for_a_new_request(stub, 0), "the first turn never started"
            sid = receiver(tmp_path)
            time.sleep(6)  # the turn is over: the session is idle, and its Stop hook has started the waiter
            before = len(stub.requests)
            send_from_another_session(monkeypatch, sid, BODY)
            assert waits_for_a_new_request(stub, before), "the idle session did not start a turn"
        finally:
            proc.kill()
            proc.communicate()
    last = requests_text(stub)[-1]
    assert "1 new message" in last  # the wake: an envelope
    assert "untrusted" in last.lower()  # and the inbox guard's delivery of what it says, marked as data


def test_an_idle_opencode_session_starts_a_turn_when_a_message_lands(tmp_path, work, monkeypatch):
    out, extra = setup(tmp_path, "opencode", monkeypatch)
    port = 4300 + os.getpid() % 500
    with OpenAIStub([], final_text="all done") as stub:
        env = gr.base_env(tmp_path) | extra
        home = tmp_path / "home"
        provider = {
            "provider": {
                "stub": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "stub",
                    "options": {"baseURL": stub.url + "/v1", "apiKey": "sk-" + "stub" * 8},
                    "models": {"m": {"name": "m"}},
                }
            }
        }
        env.update(
            OPENCODE_CONFIG_CONTENT=json.dumps({**json.loads((out / "opencode.json").read_text()), **provider}),
            OPENCODE_CONFIG_DIR=str(out),
            OPENCODE_DISABLE_AUTOUPDATE="true",
            OPENCODE_DISABLE_MODELS_FETCH="true",
            OPENCODE_DISABLE_LSP_DOWNLOAD="true",
            XDG_DATA_HOME=str(home / "data"),
            XDG_STATE_HOME=str(home / "state"),
            XDG_CACHE_HOME=os.environ.get("WS_E2E_CACHE") or str(home / "cache"),
        )
        server = subprocess.Popen(
            [gr.runner_bin("opencode"), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            env=env,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def call(method: str, path: str, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
            req.add_header("content-type", "application/json")
            data = json.dumps(body).encode() if body is not None else None
            with urllib.request.urlopen(req, data=data, timeout=60) as r:
                return json.loads(r.read() or b"null")

        try:
            end = time.monotonic() + 30
            while time.monotonic() < end:
                try:
                    call("GET", "/session")
                    break
                except OSError:
                    time.sleep(0.3)
            sess = call("POST", "/session", {})
            model = {"providerID": "stub", "modelID": "m"}
            call(
                "POST", f"/session/{sess['id']}/message", {"model": model, "parts": [{"type": "text", "text": "hello"}]}
            )
            sid = receiver(tmp_path)
            assert sid == sess["id"]
            time.sleep(2)  # the turn is over: the session is idle, and the shim's waiter is running
            before = len(stub.requests)
            send_from_another_session(monkeypatch, sid, BODY)
            assert waits_for_a_new_request(stub, before), "the idle session did not start a turn"
        finally:
            server.terminate()
            server.wait(timeout=15)
    texts = requests_text(stub)[before:]
    assert any("1 new message" in t for t in texts)  # the wake: an envelope
    assert any(BODY in t and "untrusted" in t.lower() for t in texts)  # and the body, marked as data
