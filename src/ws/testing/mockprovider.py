"""Stub model servers that script a runner's tool calls, so tests and evals need no network, paid provider or real key.

Two stubs imitate the wire formats a runner speaks (a Messages-style API and a chat-completions-style API). Each walks
the runner through a scripted list of tool calls, one per round trip, then answers in plain text. They bind to loopback
only, ask for no credentials and never echo any. `python -m ws.testing.mockprovider --scenario NAME` serves one named
scenario and prints `<VAR>=<url>` as its only stdout line, so a caller can learn the port.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import signal
import ssl
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Self

PROVIDER_VARS = {"anthropic": "MOCK_ANTHROPIC_BASE_URL", "openai": "MOCK_OPENAI_BASE_URL"}

_USAGE = {"input_tokens": 10, "output_tokens": 5}
_EMPTY_LIST = {"data": [], "object": "list"}


@dataclass(frozen=True)
class Call:
    """One scripted tool call. `tool` is the name as the runner offers it (`Bash`, `bash`); matching ignores case."""

    tool: str
    args: dict = field(default_factory=dict)


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _joined_text(content: Any) -> str:
    """The text of a tool result: the `text` of each dict item joined by spaces, or `str()` for anything else."""
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stub: _Stub  # set on the subclass made for each server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_reply(self, obj: Any) -> None:
        self._send("application/json", _compact(obj).encode())

    def sse(self, events: list[str]) -> None:
        self._send("text/event-stream", "".join(events).encode(), {"Cache-Control": "no-cache"})

    def do_GET(self) -> None:
        self.json_reply(_EMPTY_LIST)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        body = json.loads(raw) if raw.strip() else {}
        if not self.path.split("?", 1)[0].endswith(self.stub.route):
            self.json_reply({"input_tokens": 1})  # a token-count or other side call: not a model request
            return
        with self.stub._lock:
            self.stub.requests.append(body)
        self.stub.respond(self, body)


class _Stub:
    """Base of both stubs: the server, the script and the record of requests. Subclasses fill in the wire format."""

    route = ""

    def __init__(self, calls: list[Call] | None = None, final_text: str = "ok, done") -> None:
        self.calls = list(calls or [])
        self.final_text = final_text
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._scheme = "http"

    def start(self, tls: tuple[str, str] | None = None) -> _Stub:
        handler = type("Handler", (_Handler,), {"stub": self})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=tls[0], keyfile=tls[1])
            server.socket = context.wrap_socket(server.socket, server_side=True)
            self._scheme = "https"
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        assert self._server is not None, "start the stub first"
        return f"{self._scheme}://127.0.0.1:{self._server.server_address[1]}"

    def next_call(self, offered: list[str], results_seen: int) -> Call | None:
        """The call to make now: the one after the results already in the conversation, if the runner offers its tool."""
        if results_seen >= len(self.calls):
            return None
        call = self.calls[results_seen]
        return call if call.tool.lower() in {name.lower() for name in offered} else None

    def _offered_as(self, offered: list[str], call: Call) -> str:
        return next(name for name in offered if name.lower() == call.tool.lower())

    def respond(self, handler: _Handler, body: dict) -> None:
        raise NotImplementedError

    def tool_results(self) -> list[str]:
        raise NotImplementedError

    def _longest(self, per_request: list[list[str]]) -> list[str]:
        return max(per_request, key=len, default=[])  # each request repeats the earlier results


class AnthropicStub(_Stub):
    route = "/v1/messages"

    def respond(self, handler: _Handler, body: dict) -> None:
        offered = [t.get("name", "") for t in body.get("tools") or []]
        seen = sum(
            1
            for m in body.get("messages") or []
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for block in m["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        call = self.next_call(offered, seen)
        n = len(self.requests)
        if call:
            block = {"type": "tool_use", "id": f"toolu_stub{n:04d}", "name": self._offered_as(offered, call),
                     "input": call.args}  # fmt: skip
            stop = "tool_use"
        else:
            block = {"type": "text", "text": self.final_text}
            stop = "end_turn"
        shell = {
            "id": f"msg_stub{n:04d}", "type": "message", "role": "assistant", "model": body.get("model") or "stub",
            "stop_reason": None, "stop_sequence": None, "usage": dict(_USAGE),
        }  # fmt: skip
        if not body.get("stream"):
            handler.json_reply({**shell, "content": [block], "stop_reason": stop})
            return
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": _compact(call.args)}
        else:
            start = {**block, "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events = [
            ("message_start", {"type": "message_start", "message": {**shell, "content": []}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": dict(_USAGE)}),
            ("message_stop", {"type": "message_stop"}),
        ]  # fmt: skip
        handler.sse([f"event: {name}\ndata: {_compact(data)}\n\n" for name, data in events])

    def tool_results(self) -> list[str]:
        with self._lock:
            bodies = list(self.requests)
        return self._longest(
            [
                [
                    _joined_text(block.get("content"))
                    for m in body.get("messages") or []
                    if m.get("role") == "user" and isinstance(m.get("content"), list)
                    for block in m["content"]
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
                for body in bodies
            ]
        )


class OpenAIStub(_Stub):
    route = "/chat/completions"

    def respond(self, handler: _Handler, body: dict) -> None:
        offered = [(t.get("function") or {}).get("name", "") for t in body.get("tools") or []]
        seen = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        call = self.next_call(offered, seen)
        n = len(self.requests)
        if call:
            entry = {
                "index": 0, "id": f"call_stub{n:04d}", "type": "function",
                "function": {"name": self._offered_as(offered, call), "arguments": _compact(call.args)},
            }  # fmt: skip
            delta: dict = {"role": "assistant", "content": None, "tool_calls": [entry]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": self.final_text}
            finish = "stop"
        base = {"id": f"chatcmpl-stub{n:04d}", "object": "chat.completion.chunk", "created": 0,
                "model": body.get("model") or "stub"}  # fmt: skip
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            message = dict(delta)
            if call:
                message["tool_calls"] = [{k: v for k, v in e.items() if k != "index"} for e in delta["tool_calls"]]
            handler.json_reply(
                {**base, "object": "chat.completion",
                 "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
            )  # fmt: skip
            return
        chunks = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {**base, "choices": [], "usage": usage},
        ]
        handler.sse([f"data: {_compact(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"])

    def tool_results(self) -> list[str]:
        with self._lock:
            bodies = list(self.requests)
        return self._longest(
            [[_joined_text(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "tool"]
             for body in bodies]
        )  # fmt: skip


_STUBS = {"anthropic": AnthropicStub, "openai": OpenAIStub}


def for_scenario(name: str, provider: str = "anthropic", scenarios_file: str | None = None, **variables: Any) -> _Stub:
    """An unstarted stub scripted from the named scenario. An unknown provider is a `KeyError`."""
    from ws.testing import scenarios  # lazy: scenarios imports `Call` from here

    scenario = scenarios.get(name, scenarios_file)
    runner = scenarios.PROVIDERS[provider]
    return _STUBS[provider](scenario.calls_for(runner, **variables), final_text=scenario.final_text)


def main(argv: list[str] | None = None) -> int:
    from ws.testing import scenarios  # lazy, as above

    parser = argparse.ArgumentParser(
        prog="python -m ws.testing.mockprovider",
        description="Serve a scripted stand-in for a model API on loopback and print its base URL.",
    )
    parser.add_argument(
        "--scenario", required=True, help=f"scenario to serve; built in: {', '.join(scenarios.DEFAULT['scenarios'])}"
    )
    parser.add_argument("--provider", choices=sorted(PROVIDER_VARS), default="anthropic")
    parser.add_argument("--work", default=None, help="what ${work} means in the scenario's paths (default: here)")
    parser.add_argument(
        "--scenarios", default=None, help="a scenarios file (default: $WS_MOCK_SCENARIOS, else built in)"
    )
    parser.add_argument("--tls-cert", default=None, help="serve https with this certificate (needs --tls-key)")
    parser.add_argument("--tls-key", default=None, help="the certificate's private key (needs --tls-cert)")
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")

    try:
        stub = for_scenario(
            args.scenario, args.provider, args.scenarios, work=args.work if args.work is not None else os.getcwd()
        )
    except scenarios.ScenarioError as e:
        print(f"mockprovider: {e}", file=sys.stderr)
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stub.start((args.tls_cert, args.tls_key) if args.tls_cert else None)
    try:
        print(f"{PROVIDER_VARS[args.provider]}={stub.url}", flush=True)
        stop.wait()
    finally:
        stub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
