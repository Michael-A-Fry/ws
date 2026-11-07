"""AGENTS.md is read at the start of every agent session, so it stays short and holds no generated table. The code map
lives in docs/reference/code-map.md, and scripts/gen_code_map.py keeps it current from the module docstrings."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
MAP = ROOT / "docs" / "reference" / "code-map.md"
MAX_LINES = 200

spec = importlib.util.spec_from_file_location("gen_code_map", ROOT / "scripts" / "gen_code_map.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_code_map"] = gen
spec.loader.exec_module(gen)


def test_agents_md_stays_within_its_line_budget():
    lines = AGENTS.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_LINES, f"AGENTS.md has {len(lines)} lines; the limit is {MAX_LINES}"


def test_agents_md_holds_no_generated_table_and_links_the_map():
    text = AGENTS.read_text(encoding="utf-8")
    assert "code-map:start" not in text
    assert "(docs/reference/code-map.md)" in text


def test_the_generator_targets_the_page_in_the_reference():
    assert gen.TARGET == MAP
    assert MAP.is_file()


def test_the_code_map_page_is_current():
    current = MAP.read_text(encoding="utf-8")
    assert gen.updated(current) == current, "run `python3 scripts/gen_code_map.py` and commit the page"


def test_the_code_map_has_a_row_for_every_module():
    text = MAP.read_text(encoding="utf-8")
    for path in gen.PKG.glob("*.py"):
        if path.name not in gen._SKIP:
            assert f"| `{path.name}` |" in text, path.name


def test_check_exits_1_when_the_page_is_stale(tmp_path, monkeypatch, capsys):
    page = tmp_path / "code-map.md"
    page.write_text(gen.START + "\n\nstale\n\n" + gen.END + "\n", encoding="utf-8")
    monkeypatch.setattr(gen, "TARGET", page)
    assert gen.main(["--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0
