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


@pytest.mark.parametrize(
    "line",
    ["class: [alpha]", "class: Alpha", "class: nosuchclass", "class: {a: b}", "class: 7"],
)
def test_a_class_that_is_not_a_known_policy_class_drops_the_record_for_everyone(box, line):
    rec(box, "r-bad", "zebra secret", x="y")
    p = box / "ctx" / "records" / "r-bad.md"
    p.write_text(p.read_text().replace("x: y", line), encoding="utf-8")
    assert context_load.at_prompt("zebra", str(box / "work" / "a")).ids == []
    assert context_load.at_prompt("zebra", str(box / "work" / "alpha")).ids == []


def test_a_record_whose_front_matter_is_not_fully_understood_is_dropped(box):
    rec(box, "r-odd", "zebra odd", note="|")
    p = box / "ctx" / "records" / "r-odd.md"
    p.write_text("---\nid: r-odd\nstatus: current\nnested:\n  a: b\nlist: &x\n---\nzebra odd\n", encoding="utf-8")
    assert context_load.at_prompt("zebra", str(box / "work" / "a")).ids == []


def test_a_walled_session_sees_the_core_preferences_but_no_shared_record_or_other_core_file(box):
    rec(box, "r-open", "zebra public notes")
    rec(box, "r-wall", "zebra walled notes", **{"class": "alpha"})
    (box / "ctx" / "core" / "glossary.md").write_text("zebra means stripes.\n", encoding="utf-8")
    (box / "ctx" / "core" / "preferences.md").write_text("Prefer zebra answers.\n", encoding="utf-8")
    ids = context_load.at_prompt("zebra", str(box / "work" / "alpha")).ids
    assert "r-wall" in ids and "preferences" in ids and "r-open" not in ids and "glossary" not in ids


def test_an_unrelated_prompt_finds_nothing_even_with_common_words(box):
    rec(box, "r-q", "Quince jelly needs a long slow set with the sugar.")
    assert context_load.at_prompt("what should we do about the weather", str(box / "work" / "a")).ids == []
    assert context_load.at_prompt("continue", str(box / "work" / "a")).ids == []


def test_a_bad_section_loads_less_not_nothing_and_a_huge_file_is_capped(box, monkeypatch):
    monkeypatch.setattr(context_load, "card", lambda *a, **k: 1 / 0)
    assert "Prefer short" in context_load.at_start(str(box / "work" / "a")).text
    big = box / "ctx" / "core" / "preferences.md"
    big.write_text("x" * (context_load.MAX_BYTES * 3), encoding="utf-8")
    assert len(context_load._read(big)) <= context_load.MAX_BYTES


def test_a_mixed_type_rank_does_not_break_the_card(box):
    (box / "ctx" / "areas.toml").write_text(
        '[[area]]\nname = "a"\nrank = "high"\n[[area]]\nname = "b"\nrank = 2\n', encoding="utf-8"
    )
    assert context_load.card(str(box), box / "ctx").startswith("Area:")


def _hook(box, monkeypatch, capsys, event, payload):
    pin = paths.pin_of().args()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["session", "load", "--event", event, *pin]) == 0
    return capsys.readouterr().out


def _start_session(box, monkeypatch, capsys, base):
    pin = paths.pin_of().args()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(base)))
    assert cli.main(["session", "start", "--runner", "claude-code", *pin]) == 0
    capsys.readouterr()


def test_a_first_prompt_that_finds_nothing_retries_up_to_three_prompts_then_is_done(box, monkeypatch, capsys):
    rec(box, "r-quince", "Quince jelly needs a long slow set.")
    base = {"session_id": "s2", "cwd": str(box / "work" / "a")}
    _start_session(box, monkeypatch, capsys, base)
    saved = box / "wshome" / "sessions" / "s2.json"
    assert _hook(box, monkeypatch, capsys, "prompt", {**base, "prompt": "continue"}) == ""
    assert json.loads(saved.read_text()).get("context_loaded") is not True
    assert "r-quince" in _hook(box, monkeypatch, capsys, "prompt", {**base, "prompt": "quince jelly"})
    assert json.loads(saved.read_text())["context_loaded"] is True
    base3 = {"session_id": "s3", "cwd": str(box / "work" / "a")}
    _start_session(box, monkeypatch, capsys, base3)
    for _ in range(3):
        _hook(box, monkeypatch, capsys, "prompt", {**base3, "prompt": "continue"})
    assert json.loads((box / "wshome" / "sessions" / "s3.json").read_text())["context_loaded"] is True
    assert _hook(box, monkeypatch, capsys, "prompt", {**base3, "prompt": "quince jelly"}) == ""


def test_a_slow_scan_does_not_hold_the_registry_lock(box, monkeypatch, capsys):
    from ws import session

    base = {"session_id": "s4", "cwd": str(box / "work" / "a")}
    _start_session(box, monkeypatch, capsys, base)
    held = []

    def slow(message, cwd, env=None):
        with session._locked(session._record_path(paths.merged_environ(), "s4").parent) as _:
            pass  # would deadlock (flock is per open file description) if cmd_load held the lock around the scan
        held.append(True)
        return context_load.Loaded()

    monkeypatch.setattr(context_load, "at_prompt", slow)
    _hook(box, monkeypatch, capsys, "prompt", {**base, "prompt": "anything"})
    assert held
