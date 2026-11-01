"""How `ws status` prints: each path once, folders as folders, real plurals, and colour that only reinforces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import brand, cli, status
from ws.style import Style


def strip(text: str) -> str:
    return brand.ANSI_RE.sub("", text)


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("NO_COLOR", "FORCE_COLOR", "COLORFGBG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(home)
    return home


def retired_folder(root: Path, name: str = "old-skill") -> Path:
    """A skill folder with no SKILL.md and several real files, as a retired or synced one is left."""
    for part in ("notes.md", "steps.md"):
        put(root / "skills" / name / part, f"{part} " * 40)
    return root / "skills" / name


def rows_of(capsys, *extra):
    cli.main(["status", "--json", *extra])
    return json.loads(capsys.readouterr().out)["data"]["runners"]["claude-code"]


def test_a_folder_with_no_main_file_is_listed_once_and_as_a_folder(box, capsys):
    folder = retired_folder(box / ".claude")
    put(box / ".claude" / "skills" / "live" / "SKILL.md", "x" * 100)
    rows = rows_of(capsys)
    paths = [r["path"] for r in rows]
    assert len(paths) == len(set(paths))
    old = [r for r in rows if r["name"] == "old-skill"]
    assert len(old) == 1 and old[0]["path"] == str(folder) and old[0]["folder"] is True
    assert old[0]["bytes"] == sum(f.stat().st_size for f in folder.iterdir())
    live = next(r for r in rows if r["name"] == "live")
    assert live["folder"] is False and live["path"].endswith("SKILL.md")


def test_the_total_counts_a_deduplicated_row_once(box, capsys):
    folder = retired_folder(box / ".claude")
    cli.main(["status", "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    rows = data["runners"]["claude-code"]
    assert data["total_bytes"] == sum(r["bytes"] for rs in data["runners"].values() for r in rs)
    assert sum(r["bytes"] for r in rows) == sum(f.stat().st_size for f in folder.iterdir())


def test_a_folder_shows_a_trailing_slash_and_a_file_does_not(box, capsys):
    retired_folder(box / ".claude")
    put(box / ".claude" / "CLAUDE.md", "rules\n")
    cli.main(["status"])
    lines = capsys.readouterr().out.splitlines()
    assert any(line.endswith(f"{box}/.claude/skills/old-skill/") for line in lines)
    assert any(line.endswith(f"{box}/.claude/CLAUDE.md") for line in lines)


@pytest.mark.parametrize(
    ("n_rows", "first_bytes", "text"),
    [(0, 0, "0 items, 0 bytes"), (1, 1, "1 item, 1 byte"), (2, 1234, "2 items, 1,234 bytes")],
)
def test_counts_are_real_plurals(n_rows, first_bytes, text):
    rows = [{"bytes": 0, "origin": "global", "kind": "rules", "path": f"/p{i}"} for i in range(n_rows)]
    if rows:
        rows[0]["bytes"] = first_bytes
    out = status.render({"repo": "/r", "runners": {"claude-code": rows}, "errors": []})
    assert f"claude-code: {text}" in out
    assert "(s)" not in out


def test_83_items_read_as_83_items():
    rows = [{"bytes": 10, "origin": "global", "kind": "skills", "path": f"/p{i}"} for i in range(83)]
    out = status.render({"repo": "/r", "runners": {"claude-code": rows}, "errors": []})
    assert "claude-code: 83 items, 830 bytes" in out


def test_ansi_stripped_status_output_equals_its_no_color_output(box, capsys, monkeypatch):
    retired_folder(box / ".claude")
    put(box / ".claude" / "CLAUDE.md", "rules\n")
    put(box / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": []}}))
    monkeypatch.setenv("FORCE_COLOR", "1")
    cli.main(["status"])
    forced = capsys.readouterr().out
    assert "\x1b[" in forced
    cli.main(["status", "--no-color"])
    flagged = capsys.readouterr().out
    monkeypatch.delenv("FORCE_COLOR")
    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["status"])
    no_color = capsys.readouterr().out
    assert "\x1b[" not in flagged and "\x1b[" not in no_color
    assert strip(forced) == flagged == no_color
    assert "hooks: Stop" in no_color


def test_errors_are_a_glyph_and_a_word():
    data = {"repo": "/r", "runners": {}, "errors": ["bad override"]}
    assert status.render(data).splitlines()[-1] == "✗ error: bad override"
    assert strip(status.render(data, Style(brand.FULL))) == status.render(data)
