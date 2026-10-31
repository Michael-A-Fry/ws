"""Stub model servers for the Tier 0 tests: no network, no paid provider, no real key.

Two servers on 127.0.0.1 (random port), stdlib only:

    AnthropicStub(calls)   the Anthropic Messages API, SSE streaming (Claude Code)
    OpenAIStub(calls)      OpenAI-compatible /v1/chat/completions, SSE streaming (OpenCode, any compatible runner)

`calls` is the script: a list of Call(tool, args). The stub answers each request like this:

  * the request offers the first scripted tool (matched case-insensitively) and has seen N tool results
    -> if N < len(calls): reply with a tool_use for calls[N]; otherwise reply with plain text (`final_text`);
  * any other request (titles, quota probes, side calls) -> plain text.

So a script of one call runs that call once, then lets the runner finish. An empty script never calls a tool.

Every request is recorded: `stub.requests` (parsed JSON bodies, in order), `stub.tool_results()` (the text of each tool
result the runner sent back, in order: what the model "saw" after the tool ran or was blocked).

    with AnthropicStub([Call("Bash", {"command": "touch ALLOWED"})]) as stub:
        run_runner(base_url=stub.url)
        assert stub.tool_results() == [...]

Use as a context manager (or call start()/stop()). `url` is the base URL: http://127.0.0.1:PORT.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self


@dataclass(frozen=True)
class Call:
    """One scripted tool call: the tool's name as the runner offers it ("Bash" for Claude Code, "bash" for
    OpenCode) and its input."""

    tool: str
    args: dict = field(default_factory=dict)


class _Stub:
    """Shared server plumbing. A subclass sets `path_suffix` and implements respond() and tool_results()."""

    path_suffix = ""

    def __init__(self, calls: list[Call] | None = None, final_text: str = "ok, done"):
        self.calls = list(calls or [])
        self.final_text = final_text
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- life cycle
    def start(self) -> Self:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_GET(self):  # model lists, health probes
                self.json_reply({"data": [], "object": "list"})

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not self.path.split("?")[0].endswith(stub.path_suffix):
                    return self.json_reply({"input_tokens": 1})  # e.g. count_tokens
                with stub._lock:
                    stub.requests.append(body)
                stub.respond(self, body)

            def json_reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def sse(self, events: list[str]):
                data = "".join(events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # -- what the script does
    def next_call(self, offered: list[str], results_seen: int) -> Call | None:
        """The scripted call to make now, or None for plain text."""
        if not self.calls or results_seen >= len(self.calls):
            return None
        want = self.calls[results_seen].tool.lower()
        return self.calls[results_seen] if want in (o.lower() for o in offered) else None

    def respond(self, handler, body: dict) -> None:
        raise NotImplementedError

    def tool_results(self) -> list[str]:
        raise NotImplementedError


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class AnthropicStub(_Stub):
    """POST /v1/messages. Streams message_start ... message_stop; answers a non-stream request with one JSON message."""

    path_suffix = "/v1/messages"

    def respond(self, handler, body: dict) -> None:
        offered = [t.get("name", "") for t in body.get("tools") or []]
        call = self.next_call(offered, len(self._results(body)))
        n = len(self.requests)
        if call:
            blocks = [
                {
                    "type": "tool_use",
                    "id": f"toolu_stub{n:04d}",
                    "name": _real_name(offered, call.tool),
                    "input": call.args,
                }
            ]
            stop = "tool_use"
        else:
            blocks = [{"type": "text", "text": self.final_text}]
            stop = "end_turn"
        usage = {"input_tokens": 10, "output_tokens": 5}
        message = {
            "id": f"msg_stub{n:04d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "stub"),
            "stop_reason": None,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return handler.json_reply({**message, "content": blocks, "stop_reason": stop})
        events = [_sse("message_start", {"type": "message_start", "message": {**message, "content": []}})]
        for i, b in enumerate(blocks):
            if b["type"] == "tool_use":
                start = {**b, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
            else:
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": b["text"]}
            events += [
                _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
                _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
                _sse("content_block_stop", {"type": "content_block_stop", "index": i}),
            ]
        events += [
            _sse(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": usage},
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        handler.sse(events)

    @staticmethod
    def _results(body: dict) -> list[str]:
        out = []
        for m in body.get("messages") or []:
            content = m.get("content")
            if m.get("role") != "user" or not isinstance(content, list):
                continue
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out.append(str(c))
        return out

    def tool_results(self) -> list[str]:
        """Tool results in the longest conversation the stub saw (each request repeats the earlier ones)."""
        return max((self._results(r) for r in self.requests), key=len, default=[])


class OpenAIStub(_Stub):
    """POST /v1/chat/completions. Streams chat.completion.chunk events then [DONE]; non-stream gets one JSON."""

    path_suffix = "/chat/completions"

    def respond(self, handler, body: dict) -> None:
        offered = [(t.get("function") or {}).get("name", "") for t in body.get("tools") or []]
        call = self.next_call(offered, len(self._results(body)))
        n = len(self.requests)
        base = {
            "id": f"chatcmpl-stub{n:04d}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": body.get("model", "stub"),
        }
        if call:
            name = _real_name(offered, call.tool)
            tc = {
                "index": 0,
                "id": f"call_stub{n:04d}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(call.args)},
            }
            delta, finish = {"role": "assistant", "content": None, "tool_calls": [tc]}, "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": self.final_text}, "stop"
        if not body.get("stream"):
            msg = {k: v for k, v in delta.items()}
            if call:
                msg["tool_calls"] = [{k: v for k, v in tc.items() if k != "index"}]
            return handler.json_reply(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {**base, "choices": [], "usage": usage},
        ]
        handler.sse([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"])

    @staticmethod
    def _results(body: dict) -> list[str]:
        out = []
        for m in body.get("messages") or []:
            if m.get("role") == "tool":
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append(str(c))
        return out

    def tool_results(self) -> list[str]:
        return max((self._results(r) for r in self.requests), key=len, default=[])


def _real_name(offered: list[str], wanted: str) -> str:
    """The tool's name exactly as the runner offered it."""
    return next(o for o in offered if o.lower() == wanted.lower())
