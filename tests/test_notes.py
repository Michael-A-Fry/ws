"""ws note: capture into <notes>/_inbox, never overwrite, never put the notes in version control."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ws import cli, frontmatter, notes
from ws.profile import dumps, profile_path

WHEN = datetime(2026, 10, 31, 9, 30, 5, tzinfo=timezone(timedelta(hours=13)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    vault = tmp_path / "vault"
    vault.mkdir()
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text(dumps({"roles": {"notes": {"path": str(vault)}}}))
    return tmp_path


def test_capture_writes_front_matter_and_text(home):
    path = notes.capture("Ring the plumber about the tap", home / "vault", tags=["home", " ", "jobs"], now=WHEN)
    assert path.parent == home / "vault" / "_inbox"
    assert path.name == "2026-10-31-093005-ring-the-plumber-about-the-tap.md"
    doc = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert doc.clean
    assert list(doc.meta) == ["id", "created", "type", "source", "tags"]
    assert doc.meta["type"] == "note" and doc.meta["source"] == "cli"
    assert doc.meta["tags"] == ["home", "jobs"]
    assert doc.meta["created"].utcoffset() == timedelta(hours=13)
    assert len(doc.meta["id"]) == 36
    assert doc.body == "Ring the plumber about the tap\n"


def test_title_names_the_file_and_goes_in_the_front_matter(home):
    path = notes.capture("body", home / "vault", title="Q4 plan: money & time!", now=WHEN)
    assert path.name == "2026-10-31-093005-q4-plan-money-time.md"
    assert frontmatter.parse(path.read_text(encoding="utf-8")).meta["title"] == "Q4 plan: money & time!"


def test_never_overwrites_it_adds_a_suffix(home):
    a = notes.capture("same", home / "vault", now=WHEN)
    b = notes.capture("same", home / "vault", now=WHEN)
    c = notes.capture("same", home / "vault", now=WHEN)
    assert [p.name for p in (a, b, c)] == [f"2026-10-31-093005-same{s}.md" for s in ("", "-2", "-3")]
    assert len({a, b, c}) == 3 and "same" in a.read_text()


def test_a_hostile_title_cannot_leave_the_inbox(home):
    path = notes.capture("x", home / "vault", title="../../etc/passwd", now=WHEN)
    assert path.parent == home / "vault" / "_inbox"
    assert ".." not in path.name and "/" not in path.name
    odd = notes.capture("x", home / "vault", title="☃☃", now=WHEN)
    assert odd.name.endswith("-note.md")


def test_empty_text_and_missing_folder_are_clear_errors(home):
    with pytest.raises(notes.WsError, match="empty"):
        notes.capture("  \n", home / "vault")
    with pytest.raises(notes.WsError, match="not a folder"):
        notes.capture("x", home / "nope")


def test_cli_text_stdin_and_tags(home, capsys, monkeypatch):
    assert cli.main(["note", "hello", "world", "--tag", "a", "--tag", "b", "--title", "Hi"]) == 0
    first = Path(capsys.readouterr().out.strip())
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\nline two\n"))
    assert cli.main(["note", "-"]) == 0
    second = Path(capsys.readouterr().out.strip())
    assert first.is_file() and second.is_file()
    assert frontmatter.parse(first.read_text(encoding="utf-8")).meta["tags"] == ["a", "b"]
    assert second.read_text(encoding="utf-8").endswith("from stdin\nline two\n")
    assert not (home / "vault" / ".git").exists()


def test_cli_errors_exit_1_without_a_traceback(home, capsys, monkeypatch):
    assert cli.main(["note"]) == 1
    assert "nothing to note" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(" \n"))
    assert cli.main(["note", "-"]) == 1
    profile_path().write_text("")
    assert cli.main(["note", "hi"]) == 1
    assert "[roles.notes]" in capsys.readouterr().err
