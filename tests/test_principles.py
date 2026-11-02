"""docs/reference/principles.md is the rules page, and this test keeps it honest.

Every row has a unique ID, a rung (T0 to T3) and a status. A row that is `enforced` names a check that exists in the
repo, or is a `[judge]` row. A T0 row is never `[judge]`. A `planned` row is reported and passes, and its path may not
exist yet, but the count of planned rows can only fall: lower PLANNED_BASELINE when one is built. A published ID never
leaves the page; a rule that goes away stays as a `retired` row.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "reference" / "principles.md"

# The most planned rows the page may hold. A row that gets its check moves this number down in the same change.
PLANNED_BASELINE = 16

# Every ID the page has published: prefix -> how many. Raise a count when a rule is added; never lower it.
PUBLISHED = {"B": 7, "TC": 4, "U": 5, "W": 10, "F": 6, "C": 12, "D": 12, "G": 12}

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
STATUSES = {"enforced", "planned", "retired"}
ID = re.compile(r"^([A-Z]{1,2})([1-9][0-9]*)$")
CHECK_REF = re.compile(r"`([^`]+)`")
HEADING = re.compile(r"^## (.+?)\s*$")
HEADER = ["ID", "Rule", "Rung", "Check", "Status"]
RUNG_HEADER = ["Rung", "Meaning", "Where it lives"]


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.replace("|", "").strip()) <= set("-: ") and "-" in line


def scan(text: str) -> tuple[list[dict], list[str], dict[str, int]]:
    """The rows of every rules table (header `ID | Rule | Rung | Check | Status`) with the layer each sits under, the
    problems in the table layout, and how many rules tables each layer holds.

    A line that starts with `|` must belong to a table the scan recognises: a header, then a separator on the next
    line, then rows with no blank line between. Anything else (a stray row, a second header, a row after a blank line)
    is a problem, because Markdown would not render it as part of the table.
    """
    rows, errors, headers = [], [], {}
    layer, kind, state = None, None, None  # state: None, "sep" (header seen) or "rows"
    for n, line in enumerate(text.splitlines(), 1):
        heading = HEADING.match(line)
        if heading:
            layer, state = heading.group(1), None
            continue
        if not line.lstrip().startswith("|"):
            state = None
            continue
        parts = cells(line)
        if state is None:
            if parts == HEADER:
                kind, state = "rules", "sep"
                headers[layer] = headers.get(layer, 0) + 1
            elif parts == RUNG_HEADER:
                kind, state = "rung", "sep"
            else:
                errors.append(f"line {n}: a table line outside a recognised table (a blank line above it?)")
        elif state == "sep":
            if is_separator(line):
                state = "rows"
            else:
                errors.append(f"line {n}: the header needs a separator line right under it")
                state = None
        elif parts in (HEADER, RUNG_HEADER):
            errors.append(f"line {n}: a second header inside a table")
        elif kind == "rules":
            rows.append(dict(zip(HEADER, parts, strict=False), layer=layer, ncells=len(parts)))
    return rows, errors, headers


def parse(text: str) -> list[dict]:
    return scan(text)[0]


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
            out.append(f"{rid}: status {r['Status']!r} is not enforced, planned or retired")
        if not r["Rule"]:
            out.append(f"{rid}: no rule")
        if r["Status"] == "retired":
            continue  # the row only holds the ID; its check may be gone
        judge = r["Check"] == "[judge]"
        if r["Rung"] == "T0" and judge:
            out.append(f"{rid}: a T0 row needs a check, not [judge]")
        if r["Status"] == "planned" and judge:
            out.append(f"{rid}: a planned row names the check it will get")
        if not judge and not refs(r["Check"]):
            out.append(f"{rid}: Check is neither [judge] nor a `path`")
        if r["Status"] == "enforced" and not judge:  # a planned row's path may not exist yet
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


def missing_published(rows: list[dict], published: dict[str, int]) -> list[str]:
    have = {r["ID"] for r in rows}
    return [f"{p}{n}" for p, count in published.items() for n in range(1, count + 1) if f"{p}{n}" not in have]


@pytest.fixture(scope="module")
def scanned():
    return scan(PAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(scanned):
    return scanned[0]


def test_the_page_has_every_layer_with_one_rules_table_each(scanned):
    text = PAGE.read_text(encoding="utf-8")
    assert [h for h in re.findall(r"^## (.+)$", text, re.MULTILINE) if h in LAYERS] == list(LAYERS)
    assert scanned[2] == dict.fromkeys(LAYERS, 1)
    for layer in LAYERS:
        assert any(r["layer"] == layer for r in scanned[0]), f"{layer} has no rows"


def test_every_table_line_belongs_to_a_recognised_table(scanned):
    assert scanned[1] == []


def test_the_page_stays_short_enough_to_read(rows):
    assert 30 <= len(rows) <= 68


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


def test_a_published_id_never_leaves_the_page(rows):
    assert missing_published(rows, PUBLISHED) == [], "keep a removed rule as a row with status retired"
    assert {r["ID"] for r in rows} == {f"{p}{n}" for p, c in PUBLISHED.items() for n in range(1, c + 1)}, (
        "a rule was added: raise its count in PUBLISHED"
    )


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


def test_src_ws_imports_only_the_standard_library():
    """G1: every import in src/ws is the standard library or ws itself (a relative import is ws)."""
    offenders = []
    for path in sorted((ROOT / "src" / "ws").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            offenders += [
                f"{path.relative_to(ROOT)}: {n}"
                for n in names
                if n.split(".")[0] not in sys.stdlib_module_names | {"ws"}
            ]
    assert offenders == []


# The checks on the checker: each way a row or a table can be wrong is caught.


def page(*body: str) -> str:
    return "## CLI\n\n" + "\n".join(["| ID | Rule | Rung | Check | Status |", "|---|---|---|---|---|", *body]) + "\n"


def run(tmp_path, *body: str, text: str | None = None) -> list[str]:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_real.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    found_rows, errors, _ = scan(text or page(*body))
    return errors + problems(found_rows, tmp_path)


def test_a_good_page_has_no_problems(tmp_path):
    assert (
        run(
            tmp_path,
            "| C1 | Do a thing. | T0 | `tests/test_real.py::test_real` | enforced |",
            "| C2 | Weigh a thing. | T3 | [judge] | enforced |",
            "| C3 | Do a later thing. | T0 | `tests/test_later.py` | planned |",
            "| C4 | Gone. | T0 | `tests/test_gone.py` | retired |",
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


def test_a_planned_path_may_be_missing_and_an_enforced_one_may_not(tmp_path):
    assert run(tmp_path, "| C1 | Rule. | T0 | `src/ws/brand.py` | planned |") == []
    assert run(tmp_path, "| C1 | Rule. | T0 | `src/ws/brand.py` | enforced |") != []


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (
            page("| C1 | Rule. | T0 | `x` | planned |") + "\n| C2 | After a blank line. | T0 | `x` | planned |\n",
            "outside",
        ),
        (page("| C1 | Rule. | T0 | `x` | planned |") + "| A | stray | table |\n", "columns"),
        ("## CLI\n\n| ID | Rule | Rung | Check | Status |\n| C1 | No separator. | T0 | `x` | planned |\n", "separator"),
        (
            "## CLI\n\n| ID | Rule | Rung | Check | Status |\n|---|---|---|---|---|\n\n| C1 | Gap. | T0 | `x` | planned |\n",
            "outside",
        ),
        (page() + "| ID | Rule | Rung | Check | Status |\n", "second header"),
    ],
)
def test_a_table_line_outside_a_table_is_reported(tmp_path, text, fragment):
    found = run(tmp_path, text=text)
    assert any(fragment in f for f in found), found


def test_a_second_rules_table_in_a_layer_is_counted():
    text = (
        page("| C1 | Rule. | T0 | `x` | planned |")
        + "\nMore.\n\n"
        + page("| C2 | Rule. | T0 | `x` | planned |").split("\n\n", 1)[1]
    )
    assert scan(text)[2] == {"CLI": 2}


def test_a_missing_published_id_is_reported_and_a_retired_row_keeps_it():
    gone = parse(page("| C1 | Rule. | T0 | `x` | planned |"))
    assert missing_published(gone, {"C": 2}) == ["C2"]
    kept = parse(page("| C1 | Rule. | T0 | `x` | planned |", "| C2 | Gone. | T0 | `x` | retired |"))
    assert missing_published(kept, {"C": 2}) == []
