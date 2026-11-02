"""The web terminal's WebSocket: the handshake and the framing rules, including every refusal."""

from __future__ import annotations

import base64
import io
import struct

import pytest

from ws.term import websocket as w


class Wire(io.BytesIO):
    """A write side that remembers what it was given."""


def socket_for(*frames: bytes, max_message: int = 1024) -> tuple[w.WebSocket, Wire]:
    out = Wire()
    return w.WebSocket(io.BytesIO(b"".join(frames)), out, max_message=max_message), out


def client(opcode: int, payload: bytes = b"", **kw) -> bytes:
    return w.encode_frame(opcode, payload, mask=True, **kw)


def written_frames(data: bytes) -> list[tuple[int, bytes]]:
    """Split what the server wrote (unmasked frames) into (opcode, payload)."""
    out, i = [], 0
    while i < len(data):
        opcode, n = data[i] & 0x0F, data[i + 1] & 0x7F
        i += 2
        if n == 126:
            (n,) = struct.unpack(">H", data[i : i + 2])
            i += 2
        elif n == 127:
            (n,) = struct.unpack(">Q", data[i : i + 8])
            i += 8
        out.append((opcode, data[i : i + n]))
        i += n
    return out


KEY = base64.b64encode(b"the sample nonce").decode()  # the example key from RFC 6455 section 1.3


def request(**over) -> dict[str, str]:
    h = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13"}
    h["Sec-WebSocket-Key"] = KEY
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


# -- the handshake


def test_accept_key_is_the_rfc_example():
    assert w.accept_key(KEY) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_a_valid_handshake_returns_the_accept_value():
    assert w.check_handshake(request()) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize(
    "over",
    [
        {"Upgrade": None},
        {"Upgrade": "h2c"},
        {"Connection": "keep-alive"},
        {"Connection": None},
        {"Sec-WebSocket-Version": "8"},
        {"Sec-WebSocket-Version": None},
        {"Sec-WebSocket-Key": None},
        {"Sec-WebSocket-Key": "not base64!"},
        {"Sec-WebSocket-Key": "c2hvcnQ="},  # base64, but 5 bytes
        {"Sec-WebSocket-Key": "éééé"},
    ],
)
def test_a_bad_handshake_is_refused(over):
    with pytest.raises(w.HandshakeError):
        w.check_handshake(request(**over))


# -- frames in


@pytest.mark.parametrize("size", [0, 5, 125, 126, 300, 1024])
def test_text_and_binary_messages_of_every_length_form(size):
    body = bytes(range(256)) * 5
    ws, _ = socket_for(client(w.TEXT, b"a" * size), client(w.BINARY, body[:size]))
    assert ws.recv() == (w.TEXT, b"a" * size)
    assert ws.recv() == (w.BINARY, body[:size])


def test_a_64_bit_length_form_is_read():
    big = b"x" * 70000
    ws, _ = socket_for(client(w.BINARY, big), max_message=100000)
    assert ws.recv() == (w.BINARY, big)


def test_a_message_in_fragments_is_joined():
    ws, _ = socket_for(client(w.TEXT, b"he", fin=False), client(w.CONT, b"ll", fin=False), client(w.CONT, b"o"))
    assert ws.recv() == (w.TEXT, b"hello")


def test_a_ping_in_the_middle_is_answered_and_the_message_still_arrives():
    ws, out = socket_for(client(w.BINARY, b"ab", fin=False), client(w.PING, b"p"), client(w.CONT, b"cd"))
    assert ws.recv() == (w.BINARY, b"abcd")
    assert written_frames(out.getvalue()) == [(w.PONG, b"p")]


def test_a_pong_is_ignored():
    ws, _ = socket_for(client(w.PONG, b"x"), client(w.TEXT, b"after"))
    assert ws.recv() == (w.TEXT, b"after")


def test_a_close_frame_is_answered_and_ends_the_stream():
    ws, out = socket_for(client(w.CLOSE, struct.pack(">H", 1001) + b"bye"))
    assert ws.recv() is None
    assert ws.close_code == 1001 and ws.closed
    assert written_frames(out.getvalue())[0][0] == w.CLOSE


def test_a_close_with_no_code_counts_as_normal():
    ws, _ = socket_for(client(w.CLOSE))
    assert ws.recv() is None and ws.close_code == w.CLOSE_NORMAL


def test_a_dropped_connection_ends_the_stream_quietly():
    ws, _ = socket_for(client(w.TEXT, b"hello")[:4])
    assert ws.recv() is None and ws.closed


# -- every protocol error, and the close it answers with


def refused(*frames: bytes, **kw) -> tuple[int, bytes]:
    ws, out = socket_for(*frames, **kw)
    with pytest.raises(w.ProtocolError) as e:
        ws.recv()
    sent = written_frames(out.getvalue())
    assert sent and sent[-1][0] == w.CLOSE, "the error is answered with a close frame"
    assert struct.unpack(">H", sent[-1][1][:2])[0] == e.value.code
    return e.value.code, out.getvalue()


def test_an_unmasked_client_frame_is_refused():
    code, _ = refused(w.encode_frame(w.TEXT, b"hi", mask=False))
    assert code == w.CLOSE_PROTOCOL


def test_reserved_bits_are_refused():
    frame = bytearray(client(w.TEXT, b"hi"))
    frame[0] |= 0x40
    assert refused(bytes(frame))[0] == w.CLOSE_PROTOCOL


@pytest.mark.parametrize("opcode", [0x3, 0x7, 0xB, 0xF])
def test_an_unknown_opcode_is_refused(opcode):
    assert refused(client(opcode, b"x"))[0] == w.CLOSE_PROTOCOL


def test_a_fragmented_control_frame_is_refused():
    assert refused(client(w.PING, b"x", fin=False))[0] == w.CLOSE_PROTOCOL


def test_an_oversized_control_frame_is_refused():
    assert refused(client(w.PING, b"x" * 126))[0] == w.CLOSE_PROTOCOL


def test_a_continuation_with_nothing_to_continue_is_refused():
    assert refused(client(w.CONT, b"x"))[0] == w.CLOSE_PROTOCOL


def test_a_new_message_inside_a_fragmented_one_is_refused():
    assert refused(client(w.TEXT, b"a", fin=False), client(w.TEXT, b"b"))[0] == w.CLOSE_PROTOCOL


def test_a_frame_over_the_limit_is_refused_before_its_payload_is_read():
    # only the header is on the wire: a server that tried to read the 2 MB would hit the end of the stream instead
    header = bytes([0x82, 0x80 | 127]) + struct.pack(">Q", 2_000_000) + b"\x00\x00\x00\x00"
    code, _ = refused(header, max_message=1024)
    assert code == w.CLOSE_TOO_BIG


def test_fragments_that_add_up_past_the_limit_are_refused():
    chunk = b"x" * 600
    code, _ = refused(client(w.BINARY, chunk, fin=False), client(w.CONT, chunk), max_message=1024)
    assert code == w.CLOSE_TOO_BIG


def test_text_that_is_not_utf8_is_refused():
    assert refused(client(w.TEXT, b"\xff\xfe"))[0] == w.CLOSE_BAD_DATA


@pytest.mark.parametrize("code", [0, 999, 1005, 1006, 1015, 2000, 5000])
def test_a_bad_close_code_is_refused(code):
    assert refused(client(w.CLOSE, struct.pack(">H", code)))[0] == w.CLOSE_PROTOCOL


def test_a_one_byte_close_payload_is_refused():
    assert refused(client(w.CLOSE, b"x"))[0] == w.CLOSE_PROTOCOL


# -- frames out


def test_sent_messages_are_unmasked_frames():
    ws, out = socket_for()
    ws.send_text("héllo")
    ws.send_binary(b"\x00" * 300)
    ws.ping(b"hi")
    assert written_frames(out.getvalue()) == [
        (w.TEXT, "héllo".encode()),
        (w.BINARY, b"\x00" * 300),
        (w.PING, b"hi"),
    ]
    assert out.getvalue()[1] & 0x80 == 0, "a server frame has no mask bit"


def test_close_is_sent_once_and_writes_after_it_fail():
    ws, out = socket_for()
    ws.close(w.CLOSE_GOING_AWAY, "bye")
    ws.close(w.CLOSE_NORMAL)
    assert written_frames(out.getvalue()) == [(w.CLOSE, struct.pack(">H", 1001) + b"bye")]
    with pytest.raises(BrokenPipeError):
        ws.send_text("late")


def test_close_on_a_dead_connection_doesnt_raise():
    class Dead:
        def write(self, _):
            raise BrokenPipeError

    ws = w.WebSocket(io.BytesIO(), Dead())
    ws.close()
    assert ws.closed
