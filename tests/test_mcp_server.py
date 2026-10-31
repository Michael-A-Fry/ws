"""`ws mcp serve` driven as a subprocess over stdio, the way a runner would."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ws import mcp_server, memory
from ws.profile import dumps, profile_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "vault").mkdir()
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text(
        dumps({"roles": {"notes": {"path": str(tmp_path / "vault")}, "memory": {"path": str(tmp_path / "mem")}}})
    )
    memory.init(tmp_path / "mem")
    return tmp_path


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ws", "mcp", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
        self.n = 0

    def send_raw(self, line: str) -> dict:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        reply = self.send_raw(json.dumps(msg))
        assert reply["id"] == self.n
        return reply

    def tool(self, name: str, **arguments) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> tuple[int, str]:
        self.proc.stdin.close()
        err = self.proc.stderr.read()
        return self.proc.wait(timeout=20), err


@pytest.fixture
def client(home):
    c = Client()
    yield c
    if c.proc.poll() is None:
        c.close()


def test_initialize_negotiates_the_version_and_lists_tools(client):
    r = client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert r["result"]["protocolVersion"] == "2025-06-18"  # the client's, because ws supports it
    assert "tools" in r["result"]["capabilities"] and r["result"]["serverInfo"]["name"] == "ws"
    for old in ("1999-01-01", "2025-03-26", "2024-11-05"):  # unknown, or from before we dropped batching
        assert client.call("initialize", {"protocolVersion": old})["result"]["protocolVersion"] == "2026-07-28"
    # a notification gets no reply: the next answer is for the next request
    client.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    tools = client.call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["memory_search", "memory_add", "note_capture"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["required"]
        assert t["description"]
    assert client.call("ping")["result"] == {}


def test_server_discover_and_per_request_version(client):
    r = client.call("server/discover", {"_meta": {mcp_server.VERSION_KEY: "2026-07-28"}})["result"]
    assert "2026-07-28" in r["supportedVersions"] and r["resultType"] == "complete"
    assert r["capabilities"] == {"tools": {}}
    bad = client.call("tools/list", {"_meta": {mcp_server.VERSION_KEY: "1900-01-01"}})
    assert bad["error"]["code"] == -32022
    assert bad["error"]["data"]["requested"] == "1900-01-01" and "2026-07-28" in bad["error"]["data"]["supported"]


def test_add_search_and_note_capture_round_trip(client, home):
    added = client.tool("memory_add", text="Sam prefers Tuesday meetings", title="Sam", kind="person", tags=["work"])
    assert added["result"]["isError"] is False
    path = Path(added["result"]["structuredContent"]["path"])
    assert path == home / "mem" / "persons" / "sam.md" and "source: mcp" in path.read_text(encoding="utf-8")
    noted = client.tool("note_capture", text="Tuesday: book the room", tags=["todo"])
    note_path = Path(noted["result"]["structuredContent"]["path"])
    assert note_path.parent == home / "vault" / "_inbox" and "source: mcp" in note_path.read_text(encoding="utf-8")
    found = client.tool("memory_search", query="tuesday")["result"]
    assert found["isError"] is False
    results = found["structuredContent"]["results"]
    assert {r["source"] for r in results} == {"memory", "notes"}
    assert all(set(r) == {"source", "path", "title", "snippet", "score"} for r in results)
    assert found["content"][0]["type"] == "text" and "Sam" in found["content"][0]["text"]
    assert client.tool("memory_search", query="zzzznothing", limit=3)["result"]["structuredContent"] == {"results": []}


def test_a_failing_tool_says_so_in_its_result(client, home):
    r = client.tool("memory_add", text="x", kind="secret")["result"]
    assert r["isError"] is True and "kind" in r["content"][0]["text"]
    r = client.tool("memory_search", query="!!!")["result"]
    assert r["isError"] is True and "no words" in r["content"][0]["text"]
    profile_path().write_text("")  # no roles at all
    r = client.tool("note_capture", text="hi")["result"]
    assert r["isError"] is True and "[roles.notes]" in r["content"][0]["text"]


def test_bad_calls_are_errors_and_the_server_keeps_going(client):
    r = client.tool("memory_search")  # missing query
    assert r["result"]["isError"] is True and "query is required" in r["result"]["content"][0]["text"]
    for args in ({"query": 5}, {"query": "a", "limit": 0}, {"query": "a", "limit": "x"}, {"query": "a", "limit": True}):
        assert client.tool("memory_search", **args)["result"]["isError"] is True
    assert client.tool("memory_add", text="x", tags="notalist")["result"]["isError"] is True
    assert client.tool("no_such_tool")["error"]["code"] == -32602
    assert client.call("tools/call", {"name": "memory_search", "arguments": [1]})["error"]["code"] == -32602
    assert client.call("tools/call", {})["error"]["code"] == -32602
    assert client.call("tools/call", [])["error"]["code"] == -32602
    assert client.call("no/such/method")["error"]["code"] == -32601
    assert client.send_raw("{not json")["error"]["code"] == -32700
    assert client.send_raw("[1, 2]")["error"]["code"] == -32600
    assert client.send_raw('{"jsonrpc": "2.0", "id": 99}')["error"]["code"] == -32600
    assert client.send_raw('{"jsonrpc": "2.0", "id": true, "method": "ping"}')["error"]["code"] == -32600
    assert client.call("tools/list")["result"]["tools"]  # still alive after all of that
    code, err = client.close()
    assert code == 0 and "Traceback" not in err


def test_stdout_carries_only_json_rpc_and_logs_go_to_stderr(client):
    client.tool("memory_search", query="anything")
    client.proc.stdin.write("\n\n")
    client.proc.stdin.flush()
    client.call("ping")
    out = client.proc.stdout
    client.proc.stdin.close()
    rest = out.read()
    assert rest == ""  # nothing but the replies we already read
    assert client.proc.wait(timeout=20) == 0
    assert "serving on stdio" in client.proc.stderr.read()


def test_handle_line_never_raises_on_a_handler_bug(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("secret-looking detail")

    monkeypatch.setitem(mcp_server.HANDLERS, "memory_search", boom)
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_search"}})
    reply = mcp_server.handle_line(msg)
    assert reply["error"]["code"] == -32603 and "secret-looking" not in json.dumps(reply)
    assert "secret-looking" not in capsys.readouterr().err  # only the exception type is logged
