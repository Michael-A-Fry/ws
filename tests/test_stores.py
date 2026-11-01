"""docs/reference/stores.md lists every log and record file ws writes, and the code agrees with it.

A module under src/ws that writes a JSON-lines file (through `ws.jsonl`, or to a `.jsonl` path of its own) must appear
in the page's table with its schema; each line a ws writer produces carries `schema` and `ts`; and a log that used the
older `at` key writes both. The run-record schema only grows within version 2.
"""

import ast
import re
from pathlib import Path

from ws import canary, jsonl, rating, runrecord, sandbox
from ws import eval as ws_eval
from ws.guards import claims, record_calls

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ws"
PAGE = ROOT / "docs" / "reference" / "stores.md"

LOG_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.jsonl$")
WRITERS = {"append", "append_plain"}  # the functions in ws.jsonl that put a line in a file

# The schema number each log's writer declares; the page must say the same.
SCHEMAS = {
    "runs.jsonl": runrecord.SCHEMA_VERSION,
    "runs.fallback.jsonl": runrecord.SCHEMA_VERSION,
    "ratings.jsonl": rating.SCHEMA_VERSION,
    "canary.jsonl": canary.SCHEMA_VERSION,
    "results.jsonl": ws_eval.SCHEMA_VERSION,
    "looks.jsonl": ws_eval.SCHEMA_VERSION,
    "gate-calls.jsonl": record_calls.SCHEMA_VERSION,
    "claims.jsonl": claims.SCHEMA_VERSION,
    "wall-report.jsonl": sandbox.WALL_REPORT_SCHEMA,
}

# Version 2 of the run record, frozen: a later change may add a key to the schema under a new number, never to this one.
RUN_RECORD_V2 = [
    "schema",
    "run_id",
    "job",
    "runner",
    "runner_version",
    "exe_path",
    "model_requested",
    "models_used",
    "started_at",
    "ended_at",
    "duration_ms",
    "duration_api_ms",
    "exit_code",
    "status",
    "error",
    "session_id",
    "transcript_path",
    "tokens",
    "cost_usd",
    "cost_basis",
    "num_turns",
    "cwd",
    "policy",
    "secrets_used",
    "ws_version",
    "output_truncated",
    "allow_unpriced",
    "green",
    "recovered",
    "eval",
]


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table(heading: str) -> list[dict[str, str]]:
    """The rows of the first table under `## heading`, keyed by its header cells."""
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"## {heading}")
    rows, header = [], None
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            if header:
                break
            continue
        if header is None:
            header = cells(line)
        elif not set(line.replace("|", "").strip()) <= set("-: "):
            rows.append(dict(zip(header, cells(line), strict=True)))
    return rows


def listed() -> dict[str, dict[str, str]]:
    """File name -> its row, for the JSON-lines logs the page lists."""
    out = {}
    for row in table("JSON-lines logs"):
        name = row["Path under `$WS_HOME`"].strip("`").rsplit("/", 1)[-1]
        out[name] = row
    return out


def writer_modules(row: dict[str, str]) -> set[str]:
    return set(re.findall(r"`(src/ws/[\w/]+\.py)`", row["Writer"]))


def modules_that_write() -> dict[str, set[str]]:
    """Module path -> the `.jsonl` names it spells out in code (docstrings and comments left out), for every module
    under src/ws that spells one out or calls `jsonl.append` or `jsonl.append_plain`."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        names, writes = set(), False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if LOG_NAME.match(node.value):
                    names.add(node.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if node.func.attr in WRITERS and isinstance(owner, ast.Name) and owner.id == "jsonl":
                    writes = True
        if names or writes:
            found[path.relative_to(ROOT).as_posix()] = names
    return found


# Modules that spell out a .jsonl name without writing it: the policy lists ws's logs among the paths a session may
# not write, the Claude Code adapter reads the runner's own transcript, and the home API names the default path of
# the asks file it reads (it writes nothing).
NAMES_ONLY = {"src/ws/policy.py", "src/ws/adapters/claude_code.py", "src/ws/home/api.py"}
# Files another tool writes and ws only reads (the home API's default sources). Not ws stores, so stores.md doesn't
# list them; only a NAMES_ONLY module may name one.
FOREIGN = {"asks.jsonl"}


def test_every_module_that_writes_a_jsonl_file_is_listed_in_stores_md():
    rows = listed()
    allowed = {m for row in rows.values() for m in writer_modules(row)}
    found = modules_that_write()
    unlisted = sorted(set(found) - allowed - NAMES_ONLY)
    assert not unlisted, f"these write a .jsonl file but are not a Writer in docs/reference/stores.md: {unlisted}"
    for module, names in found.items():
        for name in sorted(names):
            if module in NAMES_ONLY and name in FOREIGN:
                continue
            assert name in rows or name.endswith(".old"), f"{module} names {name}, which stores.md doesn't list"


# The modules known to write a log through ws.jsonl. If the scan stops finding one, it has gone blind.
KNOWN_WRITERS = {
    "src/ws/rating.py",
    "src/ws/runrecord.py",
    "src/ws/sandbox.py",
    "src/ws/guards/claims.py",
    "src/ws/eval.py",
    "src/ws/canary.py",
    "src/ws/guards/record_calls.py",
}


def test_the_scan_finds_every_known_writer():
    missing = KNOWN_WRITERS - set(modules_that_write())
    assert not missing, f"the scan no longer sees {sorted(missing)}"


def test_ws_jsonl_is_imported_only_as_plain_jsonl_so_the_scan_can_see_the_calls():
    """`from ws import jsonl` is the one spelling the scan reads. `from ws.jsonl import append`, `import ws.jsonl`, a
    relative import or any `as` alias would let a writer slip past it."""
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "jsonl.py" and path.parent == SRC:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}" if hasattr(node, "lineno") else ""
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[-1] == "jsonl" or (
                    mod in {"ws", ""} and any(a.name == "jsonl" and a.asname for a in node.names)
                ):
                    bad.append(where)
                elif node.level and any(a.name == "jsonl" for a in node.names):
                    bad.append(where)  # `from . import jsonl`
            elif isinstance(node, ast.Import) and any(a.name.split(".")[-1] == "jsonl" for a in node.names):
                bad.append(where)
    assert not bad, f"import ws.jsonl as `from ws import jsonl`, with no alias: {bad}"


def test_each_listed_writer_module_exists_and_each_log_is_named_in_its_writer():
    for name, row in listed().items():
        mods = writer_modules(row)
        assert mods, f"{name} has no Writer module"
        for m in mods:
            assert (ROOT / m).is_file(), f"{name}: {m} is not a file"
        named = modules_that_write()
        # runs.fallback.jsonl is built from runs.jsonl's name, so only the other logs are spelled out in their writer
        assert "fallback" in name or any(name in named.get(m, set()) for m in mods), f"{name} is not named in {mods}"


def test_the_page_gives_each_log_the_schema_its_writer_declares():
    rows = listed()
    for name, schema in SCHEMAS.items():
        assert rows[name]["Schema"] == str(schema), f"{name}: stores.md says {rows[name]['Schema']}, code says {schema}"
    assert set(rows) - set(SCHEMAS) == {"broker.jsonl"}  # the one log with no schema yet; the page says so
    assert rows["broker.jsonl"]["Schema"].startswith("none")


def test_the_page_names_ts_as_the_time_key_of_every_log_that_has_one():
    for name, row in listed().items():
        keys = set(re.findall(r"`(\w+)`", row["Time key"]))
        assert keys, f"{name} names no time key"
        if name not in {"runs.jsonl", "runs.fallback.jsonl", "broker.jsonl"}:
            assert "ts" in keys, f"{name} should carry ts"
            assert keys <= {"ts", "at"}


def test_a_log_that_has_at_in_the_page_writes_ts_and_at_with_the_same_moment():
    legacy = {n for n, r in listed().items() if "`at`" in r["Time key"]}
    assert legacy == {"gate-calls.jsonl", "claims.jsonl", "wall-report.jsonl"}
    for name in legacy:
        assert "`at`" in listed()[name]["Fields"] and "`ts`" in listed()[name]["Fields"]


def test_stamped_lines_are_rfc_3339_with_a_z_and_read_back_by_when(tmp_path):
    rec = jsonl.stamped({"at": "2026-11-02T01:02:03+00:00", "x": 1}, 1, 1_000_000.0)
    p = tmp_path / "x.jsonl"
    jsonl.append(p, rec)
    (back,) = jsonl.read(p)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", back["ts"])
    assert back["schema"] == 1 and jsonl.when(back) == back["ts"]
    assert jsonl.when({"at": back["at"]}) == back["at"]  # a line from before `ts` reads the same way


def test_run_record_schema_2_only_gains_keys():
    """D4: within one schema number a record only gains fields, so a key that version 2 had is still there."""
    assert runrecord.SCHEMA_VERSION == 2
    missing = [k for k in RUN_RECORD_V2 if k not in runrecord.KEYS]
    assert not missing, f"schema 2 lost {missing}: a removed or renamed key takes a new schema number"
    assert set(runrecord.schema()["required"]) >= set(RUN_RECORD_V2)
    assert set(runrecord.schema()["properties"]) >= set(RUN_RECORD_V2)
