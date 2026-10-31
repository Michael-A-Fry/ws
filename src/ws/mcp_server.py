"""`ws mcp serve`: a small MCP server over stdio, so any runner can search and add to memory and capture notes.

Messages are newline-delimited JSON-RPC 2.0 (stdout carries nothing else; logs go to stderr). It answers both the
`initialize` handshake of the earlier revisions and `server/discover` plus per-request `_meta` of 2026-07-28. Tools
are memory_search, memory_add and note_capture. A tool that fails says so in its result (isError); a bad request is
a JSON-RPC error. Nothing here crashes the server.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from ws import __version__, memory, notes
from ws.notes import WsError
from ws.profile import ProfileError

LATEST = "2026-07-28"
SUPPORTED = (LATEST, "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"name": "ws", "version": __version__}
INSTRUCTIONS = (
    "ws memory and notes. Use memory_search before answering from memory; memory_add to keep a fact, decision, "
    "preference, person or project; note_capture for 'note this' (it goes to the person's own notes inbox)."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION = -32022

TAGS = {"type": "array", "items": {"type": "string"}, "description": "tags"}
TOOLS = [
    {
        "name": "memory_search",
        "description": "Search the person's memory files and notes. Returns matches with source, path, title, snippet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to look for"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_add",
        "description": "Remember something: writes a Markdown file to memory and commits it. Never edits old files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "what to remember"},
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": list(memory.KINDS), "default": "fact"},
                "tags": TAGS,
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "note_capture",
        "description": "Capture a note into the _inbox of the person's own notes folder (the 'note this' path).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the note"},
                "title": {"type": "string"},
                "tags": TAGS,
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


class BadArguments(Exception):
    """The tool arguments are the wrong shape; the message says which."""


def _text(args: dict, key: str, required: bool = False) -> str | None:
    v = args.get(key)
    if v is None:
        if required:
            raise BadArguments(f"{key} is required")
        return None
    if not isinstance(v, str):
        raise BadArguments(f"{key} must be a string")
    return v


def _tags(args: dict) -> list[str] | None:
    v = args.get("tags")
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        raise BadArguments("tags must be a list of strings")
    return v


def _limit(args: dict) -> int:
    v = args.get("limit", 10)
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 50:
        raise BadArguments("limit must be a whole number from 1 to 50")
    return v


def call_search(args: dict) -> tuple[str, dict]:
    hits = memory.search(_text(args, "query", True), _limit(args))
    shown = memory.render_hits(hits) or "no matches"
    return shown, {"results": [asdict(h) for h in hits]}


def call_add(args: dict) -> tuple[str, dict]:
    kind = _text(args, "kind") or "fact"
    path = memory.add(_text(args, "text", True), _text(args, "title"), kind, _tags(args), source="mcp")
    return f"remembered in {path}", {"path": str(path)}


def call_note(args: dict) -> tuple[str, dict]:
    path = notes.capture(
        _text(args, "text", True), notes.role_path("notes"), _text(args, "title"), _tags(args), source="mcp"
    )
    return f"noted in {path}", {"path": str(path)}


HANDLERS = {"memory_search": call_search, "memory_add": call_add, "note_capture": call_note}


def _result(extra: dict | None = None) -> dict:
    return {"resultType": "complete", **(extra or {})}


def tool_call(params: dict) -> dict:
    name = params.get("name")
    handler = HANDLERS.get(name) if isinstance(name, str) else None
    if handler is None:
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    try:
        text, structured = handler(args)
    except BadArguments as e:
        return _result({"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True})
    except (WsError, ProfileError) as e:
        return _result({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return _result({"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": False})


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _check_version(params: dict) -> None:
    meta = params.get("_meta")
    wanted = meta.get(VERSION_KEY) if isinstance(meta, dict) else None
    if wanted is not None and wanted not in SUPPORTED:
        raise RpcError(
            UNSUPPORTED_VERSION, "Unsupported protocol version", {"supported": list(SUPPORTED), "requested": wanted}
        )


def dispatch(method: str, params: dict) -> dict:
    _check_version(params)
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            {
                "protocolVersion": asked if asked in SUPPORTED else LATEST,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }
        )
    if method == "server/discover":
        return _result(
            {
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
                "instructions": INSTRUCTIONS,
                "ttlMs": 60000,
                "cacheScope": "public",
            }
        )
    if method == "ping":
        return {}
    if method == "tools/list":
        return _result({"tools": TOOLS})
    if method == "tools/call":
        return tool_call(params)
    raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")


def _error(id_, code: int, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def handle_line(line: str) -> dict | None:
    """One input line in, one response out (None for a notification). Never raises."""
    try:
        msg = json.loads(line)
    except ValueError:
        return _error(None, PARSE_ERROR, "parse error")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        id_ = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
            return None  # a response to something we never asked; ignore
        return _error(id_ if isinstance(id_, (str, int)) else None, INVALID_REQUEST, "invalid request")
    is_notification = "id" not in msg
    id_ = msg.get("id")
    if not is_notification and (isinstance(id_, bool) or not isinstance(id_, (str, int))):
        return _error(None, INVALID_REQUEST, "invalid request id")
    params = msg.get("params")
    if params is None:
        params = {}
    try:
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        if is_notification:
            return None  # notifications/initialized, cancelled, and the rest: nothing to answer
        return {"jsonrpc": "2.0", "id": id_, "result": dispatch(msg["method"], params)}
    except RpcError as e:
        return None if is_notification else _error(id_, e.code, e.message, e.data)
    except Exception as e:  # noqa: BLE001 (a request must not kill the server; only the type is logged)
        print(f"ws mcp: {msg['method']} failed: {type(e).__name__}", file=sys.stderr)
        return None if is_notification else _error(id_, INTERNAL_ERROR, "internal error")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    print(f"ws mcp: serving on stdio (ws {__version__})", file=sys.stderr)
    for line in stdin:
        if not line.strip():
            continue
        reply = handle_line(line)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=True, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        return serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0  # the client went away


def register(sub) -> None:
    m = sub.add_parser("mcp", help="MCP server so any runner can use ws")
    ms = m.add_subparsers(dest="mcp_command", required=True)
    s = ms.add_parser("serve", help="serve memory_search, memory_add and note_capture over stdio")
    s.set_defaults(func=cmd_serve)
