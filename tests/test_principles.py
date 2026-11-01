"""docs/reference/principles.md is the rules page, and this test keeps it honest.

Every row has a unique ID, a rung (T0 to T3) and a status. A row that is `enforced` names a check that exists in the
repo, or is a `[judge]` row. A T0 row is never `[judge]`. A `planned` row is reported and passes, but the count of
planned rows can only fall: lower PLANNED_BASELINE when one is built.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "reference" / "principles.md"

# The most planned rows the page may hold. A row that gets its check moves this number down in the same change.
PLANNED_BASELINE = 21

LAYERS = (
    "Brand and tokens",
    "Terminal colour",
    "UI",
    "UX and writing",
    "Front end",
    "CLI",
    "Data",
    "Engineering governance",
)
RUNGS = {"T0", "T1", "T2", "T3"}
STATUSES = {"enforced", "planned"}
ID = re.compile(r"^([A-Z]{1,2})([1-9][0-9]*)$")
CHECK_REF = re.compile(r"`([^`]+)`")
HEADING = re.compile(r"^## (.+?)\s*$")
HEADER = ["ID", "Rule", "Rung", "Check", "Status"]


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str) -> list[dict]:
    """Every row of every table whose header is `ID | Rule | Rung | Check | Status`, with the layer it sits under."""
    rows, layer, in_table = [], None, False
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            layer, in_table = heading.group(1), False
            continue
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        parts = cells(line)
        if parts == HEADER:
            in_table = True
            continue
        if in_table and not set(line.replace("|", "").strip()) <= set("-: "):
            rows.append(dict(zip(HEADER, parts, strict=False), layer=layer, ncells=len(parts)))
    return rows


def refs(check: str) -> list[str]:
    """The repo paths a Check cell names, each with an optional `::test_name`."""
    return [m.split("::", 1)[0] for m in CHECK_REF.findall(check)]


def problems(rows: list[dict], root: Path) -> list[str]:
    out, seen = [], {}
    for r in rows:
        rid = r["ID"]
        if r["ncells"] != len(HEADER):
            out.append(f"{rid}: {r['ncells']} columns, expected {len(HEADER)}")
            continue
        if not ID.match(rid):
            out.append(f"{rid}: not an ID like B1 or TC3")
        if rid in seen:
            out.append(f"{rid}: used twice (also in {seen[rid]})")
        seen[rid] = r["layer"]
        if r["Rung"] not in RUNGS:
            out.append(f"{rid}: rung {r['Rung']!r} is not one of T0 to T3")
        if r["Status"] not in STATUSES:
            out.append(f"{rid}: status {r['Status']!r} is not enforced or planned")
        if not r["Rule"]:
            out.append(f"{rid}: no rule")
        judge = r["Check"] == "[judge]"
        if r["Rung"] == "T0" and judge:
            out.append(f"{rid}: a T0 row needs a check, not [judge]")
        if r["Status"] == "planned" and judge:
            out.append(f"{rid}: a planned row names the check it will get")
        if not judge and not refs(r["Check"]):
            out.append(f"{rid}: Check is neither [judge] nor a `path`")
        if r["Status"] == "enforced" and not judge:
            for ref in CHECK_REF.findall(r["Check"]):
                path, _, name = ref.partition("::")
                target = (root / path).resolve()
                if root.resolve() not in target.parents or not target.is_file():
                    out.append(f"{rid}: {path} is not a file in the repo")
                elif name and not re.search(
                    rf"^\s*(?:async\s+)?def {re.escape(name)}\(", target.read_text("utf-8"), re.MULTILINE
                ):
                    out.append(f"{rid}: {path} has no test named {name}")
    return out


@pytest.fixture(scope="module")
def rows():
    return parse(PAGE.read_text(encoding="utf-8"))


def test_the_page_has_every_layer_and_they_hold_rows(rows):
    text = PAGE.read_text(encoding="utf-8")
    assert [h for h in re.findall(r"^## (.+)$", text, re.MULTILINE) if h in LAYERS] == list(LAYERS)
    for layer in LAYERS:
        assert any(r["layer"] == layer for r in rows), f"{layer} has no rows"


def test_the_page_stays_short_enough_to_read(rows):
    assert 30 <= len(rows) <= 60


def test_each_layer_uses_one_prefix_and_counts_up_without_gaps(rows):
    by_layer = {}
    for r in rows:
        m = ID.match(r["ID"])
        assert m, r["ID"]
        by_layer.setdefault(r["layer"], []).append(m.groups())
    prefixes = [{p for p, _ in ids} for ids in by_layer.values()]
    assert all(len(p) == 1 for p in prefixes), prefixes
    assert len({next(iter(p)) for p in prefixes}) == len(prefixes), "two layers share a prefix"
    for layer, ids in by_layer.items():
        assert [int(n) for _, n in ids] == list(range(1, len(ids) + 1)), layer


def test_the_page_follows_the_rules_it_states(rows):
    assert problems(rows, ROOT) == []


def test_planned_rows_are_reported_and_can_only_shrink(rows, capsys):
    planned = [r for r in rows if r["Status"] == "planned"]
    with capsys.disabled():
        print(f"\nprinciples.md: {len(planned)} planned rows (baseline {PLANNED_BASELINE})")
        for r in planned:
            print(f"  {r['ID']:<4} {r['Check']}")
    assert len(planned) <= PLANNED_BASELINE, "a planned row was added; build its check or leave it out"
    assert len(planned) == PLANNED_BASELINE, f"lower PLANNED_BASELINE to {len(planned)}"


def test_the_page_is_linked_from_the_docs_index_and_the_contributing_guide():
    for rel in ("docs/README.md", "README.md", "CONTRIBUTING.md"):
        assert "principles.md" in (ROOT / rel).read_text(encoding="utf-8"), rel


# The checks on the checker: each way a row can be wrong is caught.


def page(*body: str) -> str:
    return "## CLI\n\n" + "\n".join(["| ID | Rule | Rung | Check | Status |", "|---|---|---|---|---|", *body]) + "\n"


def run(tmp_path, *body: str) -> list[str]:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_real.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    return problems(parse(page(*body)), tmp_path)


def test_a_good_page_has_no_problems(tmp_path):
    assert (
        run(
            tmp_path,
            "| C1 | Do a thing. | T0 | `tests/test_real.py::test_real` | enforced |",
            "| C2 | Weigh a thing. | T3 | [judge] | enforced |",
            "| C3 | Do a later thing. | T0 | `tests/test_later.py` | planned |",
        )
        == []
    )


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("| C1 | Rule. | T0 | `tests/test_real.py` | enforced |", "used twice"),
        ("| C9 | Rule. | T4 | `tests/test_real.py` | enforced |", "rung"),
        ("| C9 | Rule. | T0 | `tests/test_real.py` | done |", "status"),
        ("| C9 | Rule. | T0 | [judge] | enforced |", "T0 row needs a check"),
        ("| C9 | Rule. | T1 | [judge] | planned |", "planned row names the check"),
        ("| C9 | Rule. | T0 | `tests/test_missing.py` | enforced |", "not a file"),
        ("| C9 | Rule. | T0 | `tests/test_real.py::test_gone` | enforced |", "no test named"),
        ("| C9 | Rule. | T0 | `../outside.py` | enforced |", "not a file"),
        ("| C9 | Rule. | T0 | tests/test_real.py | enforced |", "neither"),
        ("| c9 | Rule. | T0 | `tests/test_real.py` | enforced |", "not an ID"),
        ("| C9 | Rule. | T0 | `tests/test_real.py` |", "columns"),
    ],
)
def test_each_way_a_row_can_be_wrong_is_reported(tmp_path, row, fragment):
    found = run(tmp_path, "| C1 | Rule. | T0 | `tests/test_real.py` | enforced |", row)
    assert any(fragment in f for f in found), found
