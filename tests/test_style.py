"""The shared terminal style: when colour is on, a status as glyph plus word, plurals, and padding on visible text."""

from __future__ import annotations

import io

import pytest

from ws import brand, output
from ws.output import WsError
from ws.style import ASCII_GLYPHS, GLYPHS, KINDS, Style, can_encode, pad, plural


class Stream(io.StringIO):
    encoding = "utf-8"

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def strip(text: str) -> str:
    return brand.ANSI_RE.sub("", text)


# (environment, a terminal?, the --no-color flag, what colour does)
MATRIX = [
    ({}, True, False, brand.FULL),
    ({}, False, False, brand.OFF),  # a pipe or a file
    ({"TERM": "dumb"}, True, False, brand.OFF),
    ({"TERM": "xterm-256color"}, True, False, brand.FULL),
    ({"NO_COLOR": "1"}, True, False, brand.OFF),
    ({"NO_COLOR": ""}, True, False, brand.FULL),  # an empty NO_COLOR is ignored
    ({}, True, True, brand.OFF),  # --no-color
    ({"FORCE_COLOR": "1"}, False, False, brand.FULL),  # a pipe, but forced
    ({"FORCE_COLOR": "1", "TERM": "dumb"}, True, False, brand.FULL),
    ({"FORCE_COLOR": "0"}, False, False, brand.OFF),
    ({"FORCE_COLOR": "false"}, True, False, brand.FULL),  # off is the same as unset: a terminal still gets colour
    ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, True, False, brand.OFF),  # NO_COLOR wins
    ({"FORCE_COLOR": "1"}, True, True, brand.OFF),  # --no-color wins
    ({"COLORFGBG": "0;15"}, True, False, brand.BASIC),  # a light background
    ({"COLORFGBG": "15;0"}, True, False, brand.FULL),
]


@pytest.mark.parametrize(("env", "tty", "flag", "mode"), MATRIX)
def test_colour_follows_the_environment_the_terminal_and_the_flag(env, tty, flag, mode):
    style = Style.detect(Stream(tty), env, no_color=flag)
    assert style.ansi.mode == mode
    assert style.color == (mode != brand.OFF)
    painted = style.status("ok")
    assert ("\x1b[" in painted) == (mode != brand.OFF)
    assert strip(painted) == "✓ ok"


def test_a_stream_without_a_terminal_behind_it_gets_no_colour():
    class Bare:
        pass

    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    assert not Style.detect(Bare(), {}).color
    assert not Style.detect(Closed(), {}).color


def test_detect_reads_the_real_environment_when_given_none(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert not Style.detect(Stream(True)).color
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert Style.detect(Stream(True)).color


@pytest.mark.parametrize("mode", [brand.OFF, brand.BASIC, brand.FULL])
def test_status_is_a_glyph_plus_a_word_in_every_mode(mode):
    style = Style(mode)
    assert set(KINDS) == {"ok", "fail", "warn", "skip"}
    assert GLYPHS == {"ok": "✓", "fail": "✗", "warn": "!", "skip": "–"}
    for kind in KINDS:
        text = strip(style.status(kind))
        assert text == f"{GLYPHS[kind]} {kind}"  # the meaning is in the text; colour only reinforces it
    assert strip(style.status("warn", "unconfigured")) == "! unconfigured"


def test_each_status_kind_has_its_own_colour():
    style = Style(brand.FULL)
    painted = [style.status(k) for k in KINDS]
    assert len({p.split("m", 1)[0] for p in painted}) == len(KINDS)


def test_an_unknown_status_kind_is_a_mistake_in_the_caller():
    with pytest.raises(ValueError, match="unknown status kind 'unconfigured'"):
        Style().status("unconfigured")


@pytest.mark.parametrize("mode", [brand.OFF, brand.FULL])
def test_padding_counts_visible_columns_only(mode):
    style = Style(mode)
    cell = style.status("ok", width=14)
    assert len(strip(cell)) == 14
    assert pad(style.muted("ab"), 5).endswith("   ") and len(strip(pad(style.muted("ab"), 5))) == 5
    assert pad("long text", 3) == "long text"  # never cut


@pytest.mark.parametrize(
    ("n", "one", "many", "text"),
    [
        (0, "file", None, "0 files"),
        (1, "file", None, "1 file"),
        (2, "file", None, "2 files"),
        (83, "file", None, "83 files"),
        (1, "byte", None, "1 byte"),
        (1234, "byte", None, "1,234 bytes"),
        (1, "entry", "entries", "1 entry"),
        (3, "entry", "entries", "3 entries"),
    ],
)
def test_plurals(n, one, many, text):
    assert plural(n, one, many) == text


def test_key_value_rows_line_up_whatever_the_colour():
    pairs = [("repo", "/a/b"), ("runner", "claude-code"), ("n", "3")]
    plain = Style().kv(pairs, indent=2)
    assert plain == ["  repo    /a/b", "  runner  claude-code", "  n       3"]
    assert [strip(line) for line in Style(brand.FULL).kv(pairs, indent=2)] == plain
    assert Style().kv([]) == []


def test_headings_and_quiet_text_are_plain_when_colour_is_off():
    style = Style()
    assert style.heading("Title") == "Title" and style.muted("m") == "m" and style.warn("w") == "w"
    assert Style(brand.FULL).heading("Title") != "Title"


def test_an_error_reads_the_same_with_and_without_colour():
    e = WsError("usage", "no such thing", "try `ws help`", 2)
    plain = output.error_text(e)
    assert plain == "ws: no such thing\nhint: try `ws help`"
    coloured = output.error_text(e, Style(brand.FULL))
    assert coloured != plain and strip(coloured) == plain


class Encoded(io.TextIOWrapper):
    """A stream that writes bytes in a given encoding and says so, as a Windows console does."""

    def __init__(self, encoding: str) -> None:
        self.raw = io.BytesIO()
        super().__init__(self.raw, encoding=encoding, errors="strict")


@pytest.mark.parametrize(("encoding", "ascii_glyphs"), [("utf-8", False), ("cp1252", True), ("ascii", True)])
def test_a_stream_that_cannot_hold_the_glyphs_gets_ascii_ones_with_the_same_words(encoding, ascii_glyphs):
    stream = Encoded(encoding)
    style = Style.detect(stream, {})
    table = ASCII_GLYPHS if ascii_glyphs else GLYPHS
    for kind in KINDS:
        assert style.status(kind) == f"{table[kind]} {kind}"
    style.write(style.status("fail", "unconfigured") + " ✗ – done", stream)  # never raises
    stream.flush()
    assert stream.raw.getvalue().decode(encoding).startswith(f"{table['fail']} unconfigured")
    assert can_encode(stream, "".join(GLYPHS.values())) is not ascii_glyphs


def test_a_stream_with_no_encoding_is_taken_as_ascii():
    class Bare:
        pass

    assert not can_encode(Bare(), "✓") and can_encode(Bare(), "ok")
    assert Style.detect(Bare(), {}).status("ok") == "+ ok"


def test_doctor_and_status_text_survive_a_cp1252_console(tmp_path, monkeypatch):
    from ws import doctor, osinfo
    from ws.doctor import Check

    osi = osinfo.OSInfo(kind="windows", release="11", arch="x86_64", scheduler="none", supported=False)
    checks = [Check("WS001", "platform", doctor.FAIL, "native Windows is not supported · use WSL2", "install WSL2")]
    stream = Encoded("cp1252")
    style = Style.detect(stream, {})
    style.write(doctor.render(checks, osi, style), stream)
    stream.flush()
    out = stream.raw.getvalue().decode("cp1252")
    assert "x fail" in out and "WS001" in out and "✗" not in out
