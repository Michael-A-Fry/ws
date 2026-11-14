"""ws.context_load and `ws session load`: preferences, the area card, the first-prompt records, the wall, the budget."""

from __future__ import annotations

import io
import json
import sys

import pytest

from ws import cli, context_load, paths


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    ctx = tmp_path / "ctx"
    monkeypatch.setenv("WS_CONTEXT_DIR", str(ctx))
    for d in ("a", "b", "alpha", "loose"):
        (tmp_path / "work" / d).mkdir(parents=True)
    (tmp_path / "wshome").mkdir()
    (tmp_path / "wshome" / "policy.toml").write_text(
        f'[classes.alpha]\naccess = "walled"\nrepos = ["{tmp_path}/work/alpha/**"]\n', encoding="utf-8"
    )
    w = tmp_path / "work"
    (ctx / "core").mkdir(parents=True)
    (ctx / "core" / "preferences.md").write_text("Prefer short answers.\n", encoding="utf-8")
    (ctx / "areas.toml").write_text(
        f'''[[area]]
name = "area-a"
folders = ["{w}/a"]
rank = 1
now = ["ship one", "ship two", "ship three", "ship four"]
left = "2 days"
dated = ["tax 30 Nov"]
[[area]]
name = "area-b"
folders = ["{w}/b"]
rank = 2
[[area]]
name = "inbox"
state = "inbox"
folders = []
''',
        encoding="utf-8",
    )
    return tmp_path


def rec(box, name, body, **meta):
    meta = {"id": name, "title": name, "status": "current", **meta}
    head = "\n".join(f"{k}: {v}" for k, v in meta.items())
    p = box / "ctx" / "records" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{head}\n---\n{body}\n", encoding="utf-8")


def test_start_prints_preferences_and_the_area_card_of_five_lines_at_most(box):
    out = context_load.at_start(str(box / "work" / "a")).text
    assert "Prefer short answers." in out
    card = [ln for ln in out.split("\n\n")[1].splitlines()]
    assert card[0] == "Area: area-a, 1 of 2" and len(card) <= 5
    assert "ship three" in card[1] and "ship four" not in out and "tax 30 Nov" in out


def test_a_folder_in_no_area_gets_the_inbox_card_and_says_so(box):
    out = context_load.at_start(str(box / "work" / "loose")).text
    assert "Area: inbox (this folder is in no area" in out


def test_a_planted_preference_and_matching_record_load_with_their_ids(box):
    rec(box, "r-quince", "Quince jelly needs a long slow set.")
    rec(box, "r-old", "Quince is old news.", status="superseded")
    got = context_load.at_prompt("how do I set quince jelly", str(box / "work" / "a"))
    assert got.ids == ["r-quince"] and "Quince jelly" in got.text and "old news" not in got.text


def test_another_areas_record_never_loads_and_the_shared_core_does(box):
    rec(box, "r-b", "marmalade secret", area="area-b")
    rec(box, "r-a", "marmalade method", area="area-a")
    (box / "ctx" / "core" / "glossary.md").write_text("marmalade means the jam.\n", encoding="utf-8")
    got = context_load.at_prompt("marmalade", str(box / "work" / "a"))
    assert "r-b" not in got.ids and "r-a" in got.ids and "glossary" in got.ids


def test_a_walled_record_never_appears_in_a_shared_session_and_walled_sees_only_its_own(box):
    rec(box, "r-wall", "zebra budget figures", **{"class": "alpha"})
    rec(box, "r-open", "zebra public notes")
    (box / "ctx" / "index").mkdir()
    (box / "ctx" / "index" / "alpha.md").write_text("WALLED-INDEX-LINE\n", encoding="utf-8")
    (box / "ctx" / "index" / "shared.md").write_text("SHARED-INDEX-LINE\n", encoding="utf-8")
    shared = str(box / "work" / "a")
    assert context_load.at_prompt("zebra", shared).ids == ["r-open"]
    start = context_load.at_start(shared).text
    assert "SHARED-INDEX-LINE" in start and "WALLED" not in start
    wall = str(box / "work" / "alpha")
    assert context_load.at_prompt("zebra", wall).ids == ["r-wall"]
    start = context_load.at_start(wall).text
    assert "WALLED-INDEX-LINE" in start and "SHARED-INDEX-LINE" not in start


def test_a_policy_that_will_not_load_gives_core_only(box):
    rec(box, "r-open", "zebra public notes")
    (box / "wshome" / "policy.toml").write_text('[classes.alpha]\naccess = "nonsense"\n', encoding="utf-8")
    assert context_load.at_prompt("zebra", str(box / "work" / "a")).ids == []
    assert "Prefer short" in context_load.at_start(str(box / "work" / "a")).text


def test_the_start_text_is_cut_at_the_budget(box):
    (box / "ctx" / "core" / "preferences.md").write_text("word " * 5000, encoding="utf-8")
    out = context_load.at_start(str(box / "work" / "a")).text
    assert len(out) <= context_load.BUDGET and "cut at the size limit" in out


def test_the_hook_loads_once_and_records_the_ids(box, monkeypatch, capsys):
    rec(box, "r-quince", "Quince jelly needs a long slow set.")
    w = str(box / "work" / "a")
    pin = paths.pin_of().args()

    def run(event, payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert cli.main(["session", "load", "--event", event, "--runner", "claude-code", *pin]) == 0
        return capsys.readouterr().out

    base = {"session_id": "s1", "cwd": w}
    assert "Prefer short" in run("start", base)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(base)))
    assert cli.main(["session", "start", "--runner", "claude-code", *pin]) == 0
    capsys.readouterr()
    assert "r-quince" in run("prompt", {**base, "prompt": "quince jelly please"})
    assert run("prompt", {**base, "prompt": "quince jelly again"}) == ""
    saved = json.loads((box / "wshome" / "sessions" / "s1.json").read_text())
    assert saved["context_ids"] == ["r-quince"] and saved["context_loaded"] is True
