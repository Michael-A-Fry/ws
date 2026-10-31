"""The front matter reader and writer: a safe YAML subset, and nothing crashes on the rest."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from ws import frontmatter as fm


def test_reads_scalars_lists_and_dates():
    text = (
        "---\n"
        "id: abc-123\n"
        "title: Use SQLite for the index\n"
        "n: 42\n"
        "ratio: 0.5\n"
        "on: true\n"
        "off: False\n"
        "nothing: null\n"
        "empty:\n"
        "day: 2026-10-31\n"
        "created: 2026-10-31T09:30:00+13:00\n"
        "utc: 2026-10-31T09:30:00Z\n"
        "tags: [a, 'b c', \"d, e\"]\n"
        "more:\n"
        "  - x\n"
        "  - 2026-01-02\n"
        "# a comment\n"
        "note: plain # trailing comment\n"
        "---\n"
        "\n"
        "Body text.\n"
    )
    d = fm.parse(text)
    assert d.clean and d.unparsed == []
    assert d.meta["id"] == "abc-123"
    assert d.meta["title"] == "Use SQLite for the index"
    assert (d.meta["n"], d.meta["ratio"], d.meta["on"], d.meta["off"]) == (42, 0.5, True, False)
    assert d.meta["nothing"] is None and d.meta["empty"] is None
    assert d.meta["day"] == date(2026, 10, 31)
    assert d.meta["created"].utcoffset().total_seconds() == 13 * 3600
    assert d.meta["utc"] == datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
    assert d.meta["tags"] == ["a", "b c", "d, e"]
    assert d.meta["more"] == ["x", date(2026, 1, 2)]
    assert d.meta["note"] == "plain"
    assert d.body == "Body text.\n"
    assert list(d.meta)[:3] == ["id", "title", "n"]


def test_quoted_strings_keep_what_looks_like_other_types():
    d = fm.parse("---\na: \"2026-01-01\"\nb: 'true'\nc: \"line\\nbreak \\u00e9 \\\"q\\\"\"\nd: 'it''s'\n---\nx")
    assert d.meta == {"a": "2026-01-01", "b": "true", "c": 'line\nbreak é "q"', "d": "it's"}


def test_no_front_matter_or_unclosed_fence_is_all_body():
    for text in ("just text\n", "", "---\nkey: v\nno closing fence\n", "--- not a fence\nx\n"):
        d = fm.parse(text)
        assert d.raw is None and d.meta == {} and d.body == text


def test_complex_yaml_is_kept_verbatim_and_the_rest_still_reads():
    text = (
        "---\n"
        "title: Fine\n"
        "nested:\n"
        "  inner: 1\n"
        "  other: 2\n"
        "block: |\n"
        "  line one\n"
        "  line two\n"
        "anchor: &a value\n"
        "flow: {a: 1}\n"
        "multi: first\n"
        "  continued\n"
        'bad: "unterminated\n'
        "tags: [x, y]\n"
        "---\n"
        "body\n"
    )
    d = fm.parse(text)
    assert not d.clean
    assert d.meta == {"title": "Fine", "tags": ["x", "y"]}
    assert len(d.unparsed) == 6
    assert d.unparsed[0] == "nested:\n  inner: 1\n  other: 2"
    assert d.unparsed[1] == "block: |\n  line one\n  line two"
    assert d.raw is not None and "inner: 1" in d.raw and "&a value" in d.raw
    assert d.body == "body\n"
    # writing it back keeps the unparsed chunks verbatim
    out = fm.dumps(d.meta, d.body, d.unparsed)
    for chunk in d.unparsed:
        assert chunk in out
    assert fm.parse(out).meta == d.meta


def test_crlf_bom_and_a_line_with_no_key():
    d = fm.parse("﻿---\r\ntitle: A\r\nwhat is this line\r\n---\r\nbody\r\n")
    assert d.meta == {"title": "A"} and d.unparsed == ["what is this line"]
    assert d.body == "body\n"


def test_dumps_round_trips_awkward_values():
    meta = {
        "id": "a-b_c.d",
        "plain": "hello world",
        "colon": "a: b",
        "hash": "x # y",
        "quote": 'say "hi"',
        "slash": "back\\slash",
        "newline": "one\ntwo\ttab",
        "unicode": "café ☃",
        "ctrl": "bell\x07 nel\x85",
        "empty": "",
        "padded": " lead",
        "lookalikes": ["true", "no", "null", "12", "1.5", "2026-10-31", "2026-10-31T01:02:03", "~", "- dash", "[x]"],
        "bool": True,
        "int": -7,
        "float": 2.5,
        "none": None,
        "day": date(2026, 10, 31),
        "when": datetime(2026, 10, 31, 9, 30, 5, tzinfo=UTC),
        "tags": ["a", "b c", "d, e", "[f]"],
        "no_tags": [],
    }
    out = fm.dumps(meta, "Body\n\nmore")
    assert len(out.split("---\n")[1].splitlines()) == len(meta)  # one line per key, whatever the values hold
    d = fm.parse(out)
    assert d.clean, d.unparsed
    assert d.meta == meta
    assert d.body == "Body\n\nmore\n"
    assert fm.dumps(d.meta, d.body) == out  # stable on a second pass


def test_dumps_refuses_what_it_cannot_write_safely():
    with pytest.raises(ValueError):
        fm.dumps({"bad key": 1})
    with pytest.raises(TypeError):
        fm.dumps({"nested": {"a": 1}})
    with pytest.raises(TypeError):
        fm.dumps({"deep": [[1]]})


def test_garbage_never_crashes():
    rng = random.Random(7)  # fixed seed: the same junk every run
    pieces = ["---", "...", "a: b", "- x", "  - y", "k: [", "k: ]", '"', "'", "&", "*x", "|", ">", "{", "}", ":", "#"]
    pieces += ["\t", "", "é"]
    for _ in range(500):
        lines = [rng.choice(pieces) + rng.choice(["", " ", ": v", " [1,2]"]) for _ in range(rng.randint(0, 12))]
        d = fm.parse("\n".join(lines))
        assert isinstance(d.meta, dict) and isinstance(d.body, str)
