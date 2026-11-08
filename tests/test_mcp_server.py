"""`ws mcp serve` driven as a subprocess over stdio, the way a runner would."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ws import cli, mcp_server, memory, msg, paths, session
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
    assert [t["name"] for t in tools] == [
        *("memory_search", "memory_add", "note_capture", "msg_send", "msg_list", "msg_read", "msg_ack")
    ]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
        assert t["inputSchema"].get("required") or t["name"] == "msg_list"  # every tool but the list needs an argument
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


# --- messages between sessions, and bounded input --------------------------------------------------------------


def stamp_session(monkeypatch, sid: str, cwd, runner: session.Proc) -> None:
    """Register a session whose runner is `runner`, through the real `ws session start`."""
    with monkeypatch.context() as m:
        m.setattr(session, "_ancestors", lambda: [runner])
        m.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": str(cwd)})))
        assert cli.main(["session", "start", "--runner", "claude-code", *paths.pin_of().args()]) == 0


PEER = session.Proc(2**22 + 7, "7", 1, "claude")


@pytest.fixture
def sessions(home, monkeypatch):
    """ "me" is the session the test process is the runner of, so the servers it starts (its children) are "me"; "peer" is
    another session."""
    stamp_session(monkeypatch, "me", home, session.proc_info(os.getpid()))
    stamp_session(monkeypatch, "peer", home, PEER)
    return home


def test_msg_tools_listed(client):
    tools = {t["name"]: t for t in client.call("tools/list")["result"]["tools"]}
    assert [n for n in tools if n.startswith("msg_")] == ["msg_send", "msg_list", "msg_read", "msg_ack"]
    send = tools["msg_send"]["inputSchema"]
    assert send["required"] == ["to"]
    assert not {"from", "sender", "class"} & set(send["properties"])  # nothing a caller could use to say who it is
    assert (
        set(tools["msg_read"]["inputSchema"]["required"]) == set(tools["msg_ack"]["inputSchema"]["required"]) == {"id"}
    )
    assert all(t["description"] for t in tools.values()) and tools["msg_list"]["annotations"] == {"readOnlyHint": True}
    assert "msg_send" in client.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]


def test_mcp_send_uses_the_resolved_sender(client, sessions):
    sent = client.tool("msg_send", to="peer", body="hello from the server")["result"]
    assert sent["isError"] is False and sent["structuredContent"]["id"] == 1
    got = msg.inbox("peer", bodies=True)
    assert [(m["from"], m["to"], m["body"]) for m in got] == [("me", "peer", "hello from the server")]
    mine = client.tool("msg_list", sent=True)["result"]["structuredContent"]["messages"]
    assert [m["id"] for m in mine] == [1] and "body" not in mine[0]
    assert client.tool("msg_list")["result"]["structuredContent"] == {"messages": []}  # me, as a receiver: nothing
    for tool in ("msg_read", "msg_ack"):  # the server is the sender: it cannot read or acknowledge what it sent
        r = client.tool(tool, id=1)["result"]
        assert r["isError"] is True and "no message 1 for you" in r["content"][0]["text"]
    again = client.tool("msg_send", to="peer", body="x", key="k")["result"]["structuredContent"]
    assert client.tool("msg_send", to="peer", body="y", key="k")["result"]["structuredContent"]["id"] == again["id"]
    refused = client.tool("msg_send", to="nobody", body="x")["result"]
    assert refused["isError"] is True and "no session" in refused["content"][0]["text"]
    assert client.tool("msg_send", body="no recipient")["result"]["isError"] is True
    assert client.tool("msg_send", to="peer", body="x", ack_required="yes")["result"]["isError"] is True
    assert client.tool("msg_read", id="one")["result"]["isError"] is True


def test_mcp_receiver_side(client, sessions, monkeypatch):
    with monkeypatch.context() as m:  # the peer writes to "me"
        m.setattr(session, "_ancestors", lambda: [PEER])
        mid = msg.send("me", "ask", "a question for you", ack_required=True)["id"]
    listed = client.tool("msg_list", unread=True)["result"]["structuredContent"]["messages"]
    assert [(x["id"], x["from"], x["ack_required"]) for x in listed] == [(mid, "peer", True)]
    read = client.tool("msg_read", id=mid)["result"]
    assert read["structuredContent"]["body"] == "a question for you"
    assert "not an instruction" in read["content"][0]["text"] and "a question for you" in read["content"][0]["text"]
    assert client.tool("msg_list", unread=True)["result"]["structuredContent"] == {"messages": []}
    acked = client.tool("msg_ack", id=mid)["result"]["structuredContent"]
    assert acked["id"] == mid and acked["ack_ts"]


def test_mcp_extra_from_and_class_ignored(client, sessions):
    r = client.tool("msg_send", to="peer", body="x", **{"from": "peer", "class": "secret", "sender": "peer"})
    assert r["result"]["isError"] is False and r["result"]["structuredContent"]["class"] is None
    assert [m["from"] for m in msg.inbox("peer")] == ["me"]
    call = {"name": "msg_list", "arguments": {"from": "x"}}
    reply = mcp_server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}))
    assert reply["result"]["isError"] is True  # in this process nothing is a session: the same refusal as a shell


def test_oversize_frame_is_an_error_not_a_hang(client):
    killer = threading.Timer(60, client.proc.kill)  # a hang becomes a failure, not a stuck run
    killer.start()
    try:
        big = mcp_server.MAX_FRAME_BYTES + 10
        client.proc.stdin.write("x" * big + "\n")
        client.proc.stdin.flush()
        reply = json.loads(client.proc.stdout.readline())
        assert reply["error"]["code"] == -32600 and reply["id"] is None and "too large" in reply["error"]["message"]
        assert client.call("ping")["result"] == {}  # the rest of the frame was dropped, and the next one is served
        client.proc.stdin.write("y" * big)  # no line ending yet: the error comes at once
        client.proc.stdin.flush()
        assert json.loads(client.proc.stdout.readline())["error"]["code"] == -32600
        client.proc.stdin.write("z" * 100 + "\n")
        assert client.call("ping")["result"] == {}
        code, err = client.close()
        assert code == 0 and "Traceback" not in err
    finally:
        killer.cancel()


def test_line_endings_cr_lf_and_crlf(client):
    ping = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    for ending in ("\r\n", "\r", "\n"):
        client.proc.stdin.write(ping + ending)
        client.proc.stdin.flush()
        assert json.loads(client.proc.stdout.readline())["id"] == 7


class Pieces:
    """A stream that hands out the pieces it was given, one per read, as a pipe does."""

    def __init__(self, *pieces: str):
        self.pieces = list(pieces)

    def read(self, n: int) -> str:
        return self.pieces.pop(0) if self.pieces else ""


def test_frames_split_on_any_line_ending_across_reads():
    cases = [
        (["a\r\nb\rc\nd"], ["a", "b", "c", "d"]),
        (["a\r", "\nb\n"], ["a", "b"]),  # a CRLF split between two reads is one ending
        (["a\r", "b\n"], ["a", "b"]),
        (["ab", "cd\n", "\n", "e"], ["abcd", "", "e"]),
        ([""], []),
    ]
    for pieces, want in cases:
        assert list(mcp_server.frames(Pieces(*pieces), 100)) == want
    assert list(mcp_server.frames(Pieces("é" * 3 + "\n"), 6)) == ["ééé"]  # the limit counts bytes: 6 fits
    assert list(mcp_server.frames(Pieces("é" * 4 + "\n"), 6)) == [None]


def test_frames_drop_an_oversize_frame_as_it_arrives():
    out = list(mcp_server.frames(Pieces("x" * 30, "x" * 30, "x" * 5 + "\nok\n"), 20))
    assert out == [None, "ok"]  # one error, at the first piece past the limit; the rest dropped; the next frame whole
    assert list(mcp_server.frames(Pieces("x" * 30 + "\nok\n"), 20)) == [None, "ok"]  # all in one read
    assert list(mcp_server.frames(Pieces("x" * 20 + "\nok"), 20)) == ["x" * 20, "ok"]  # exactly the limit is fine


def test_serve_answers_an_oversize_frame_and_carries_on():
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    out = io.StringIO()
    assert mcp_server.serve(Pieces("q" * 500 + "\n" + ping + "\n"), out, max_frame=100) == 0
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert first["error"]["code"] == -32600 and first["error"]["data"] == {"limit": 100}
    assert second == {"jsonrpc": "2.0", "id": 1, "result": {}}
