"""A small WebSocket (RFC 6455) for the web terminal: the opening handshake, framing, ping and close. Stdlib only.

The server end only. `check_handshake` validates a client's upgrade request and `accept_key` makes the answer; `WebSocket`
reads and writes frames over a pair of file objects. A client frame must be masked, a reserved bit, an unknown opcode, a
fragmented control frame, a bad close code or text that isn't UTF-8 is a protocol error, and a message over
`max_message` bytes is refused before it is read, so one client can't make the server buffer without limit. Writes are
locked, so the output thread and the reader can both send.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import struct
import threading
from collections.abc import Mapping
from typing import BinaryIO

__all__ = [
    "BINARY",
    "CLOSE_GOING_AWAY",
    "CLOSE_NORMAL",
    "CLOSE_PROTOCOL",
    "CLOSE_TOO_BIG",
    "TEXT",
    "HandshakeError",
    "ProtocolError",
    "WebSocket",
    "accept_key",
    "check_handshake",
    "encode_frame",
]

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
CLOSE_NORMAL, CLOSE_GOING_AWAY, CLOSE_PROTOCOL, CLOSE_BAD_DATA, CLOSE_TOO_BIG = 1000, 1001, 1002, 1007, 1009
MAX_MESSAGE = 4 * 1024 * 1024
# Close codes a peer may send: the registered ones except the three that mean "no status was sent", plus 3000-4999.
_BAD_CODES = {1004, 1005, 1006, 1015}


class HandshakeError(ValueError):
    """The upgrade request isn't a valid WebSocket handshake; the person's browser gets a 400."""


class ProtocolError(Exception):
    """The peer broke the framing rules. `code` is the close code to answer with."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def check_handshake(headers: Mapping[str, str]) -> str:
    """Validate the upgrade request's headers (any mapping with case-insensitive `.get`) and return the accept value."""
    if (headers.get("Upgrade") or "").strip().lower() != "websocket":
        raise HandshakeError("Upgrade must be websocket")
    tokens = {t.strip().lower() for t in (headers.get("Connection") or "").split(",")}
    if "upgrade" not in tokens:
        raise HandshakeError("Connection must include Upgrade")
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        raise HandshakeError("Sec-WebSocket-Version must be 13")
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    try:
        ok = len(base64.b64decode(key.encode("ascii"), validate=True)) == 16
    except (binascii.Error, UnicodeError):
        ok = False
    if not ok:
        raise HandshakeError("Sec-WebSocket-Key must be 16 random bytes, base64-encoded")
    return accept_key(key)


def encode_frame(opcode: int, payload: bytes, *, mask: bool = False, fin: bool = True) -> bytes:
    """One frame. A server writes unmasked; `mask=True` is for a client (the tests)."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        head.append(flag | n)
    elif n < 65536:
        head += bytes([flag | 126]) + struct.pack(">H", n)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def _unmask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


class WebSocket:
    """Frames over `rfile` and `wfile`. `recv()` returns `(opcode, payload)` for a text or binary message, or None once
    the connection is closed (the peer's close frame is answered)."""

    def __init__(self, rfile: BinaryIO, wfile: BinaryIO, *, max_message: int = MAX_MESSAGE):
        self.rfile, self.wfile, self.max_message = rfile, wfile, max_message
        self._lock = threading.Lock()
        self.closed = False
        self.close_code: int | None = None

    # -- writing
    def _write(self, data: bytes) -> None:
        with self._lock:
            if self.closed:
                raise BrokenPipeError("the WebSocket is closed")
            self.wfile.write(data)
            flush = getattr(self.wfile, "flush", None)
            if flush:
                flush()

    def send_text(self, text: str) -> None:
        self._write(encode_frame(TEXT, text.encode("utf-8")))

    def send_binary(self, data: bytes) -> None:
        self._write(encode_frame(BINARY, data))

    def ping(self, data: bytes = b"") -> None:
        self._write(encode_frame(PING, data[:125]))

    def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Send a close frame once, then refuse further writes. Safe to call twice and after the peer has gone."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.wfile.write(encode_frame(CLOSE, struct.pack(">H", code) + reason.encode("utf-8")[:120]))
                flush = getattr(self.wfile, "flush", None)
                if flush:
                    flush()
            except OSError:
                pass

    # -- reading
    def _read(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise ConnectionError("the peer closed the connection")
        return data

    def _frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        fin, rsv, opcode = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        if rsv:
            raise ProtocolError(CLOSE_PROTOCOL, "reserved bits set")
        if opcode not in (CONT, TEXT, BINARY, CLOSE, PING, PONG):
            raise ProtocolError(CLOSE_PROTOCOL, "unknown opcode")
        if not b1 & 0x80:
            raise ProtocolError(CLOSE_PROTOCOL, "client frames must be masked")
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack(">H", self._read(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", self._read(8))
        if opcode >= CLOSE and (n > 125 or not fin):
            raise ProtocolError(CLOSE_PROTOCOL, "bad control frame")
        if n > self.max_message:
            raise ProtocolError(CLOSE_TOO_BIG, "frame too big")
        key = self._read(4)
        return fin, opcode, _unmask(self._read(n), key)

    def recv(self) -> tuple[int, bytes] | None:
        kind: int | None = None
        parts: list[bytes] = []
        size = 0
        try:
            while True:
                fin, opcode, payload = self._frame()
                if opcode == PING:
                    self._write(encode_frame(PONG, payload))
                elif opcode == PONG:
                    pass
                elif opcode == CLOSE:
                    self._on_close(payload)
                    return None
                elif opcode == CONT:
                    if kind is None:
                        raise ProtocolError(CLOSE_PROTOCOL, "unexpected continuation")
                    parts.append(payload)
                    size += len(payload)
                else:
                    if kind is not None:
                        raise ProtocolError(CLOSE_PROTOCOL, "new message inside a fragmented one")
                    kind, parts, size = opcode, [payload], len(payload)
                if size > self.max_message:
                    raise ProtocolError(CLOSE_TOO_BIG, "message too big")
                if fin and opcode in (CONT, TEXT, BINARY):
                    data = b"".join(parts)
                    if kind == TEXT:
                        try:
                            data.decode("utf-8")
                        except UnicodeDecodeError:
                            raise ProtocolError(CLOSE_BAD_DATA, "text is not UTF-8") from None
                    return kind, data
        except ProtocolError as e:
            self.close(e.code, str(e))
            raise
        except (ConnectionError, OSError):
            self.closed = True
            return None

    def _on_close(self, payload: bytes) -> None:
        if len(payload) == 1:
            raise ProtocolError(CLOSE_PROTOCOL, "bad close frame")
        code = struct.unpack(">H", payload[:2])[0] if payload else CLOSE_NORMAL
        if code in _BAD_CODES or code < 1000 or 1015 < code < 3000 or code > 4999:
            raise ProtocolError(CLOSE_PROTOCOL, "bad close code")
        self.close_code = code
        self.close(code)
