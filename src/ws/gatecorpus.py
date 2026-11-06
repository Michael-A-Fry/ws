"""The gate's replay corpus: labelled calls, and the numbers that say how well the gate sorts them.

A corpus is JSON lines, schema `ws.gate-corpus/1`, one row each:

    {"id": "oracle-0001", "payload": {"kind": "command", "value": "cat ~/.ssh/id_rsa"}, "expect": "deny",
     "source": "oracle", "why": "a private key", "known": false}

`payload` is a call in runner-neutral terms (`kind` is command, read, write or search; `value` the command or the path;
a write may carry `content`), which ws writes as each runner's own hook payload, so one file serves every runner. A row
for one runner's own payload instead carries that payload and a `runner`. `expect` is the decision a person would want:
allow, deny or ask. `source` says where the row came from (oracle, parity, regression, control or recorded). `known`
marks a row the gate gets wrong today, with the reason in `why`: it is counted but does not fail, and a known row that
starts passing fails until it is unmarked, so a fix shows up and the list of known rows only ever shrinks. `guards`
names guards to switch on for that row alone.

Paths use `~`, `$HOME`, `{HOME}` and `{CWD}`. Replay fills `{HOME}` and `{CWD}` with scratch folders it makes, and runs
with `HOME` pointing at the first, so a corpus names no real path and gives the same answer on any machine. (`${HOME}`
is the shell's own and is left alone.)

The numbers are a plain classifier's: a confusion matrix, the false-block rate (a call that should run, stopped or held),
the miss rate (a call that should stop, allowed), how often a deny came back as an ask, each with a Wilson 95% interval.
Standard library only; the gate does the judging (`ws.gate.replay_corpus`) and passes the results here.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ws.evalstats import percentile
from ws.output import WsError

SCHEMA = "ws.gate-corpus/1"
SNAPSHOT_SCHEMA = "ws.gate-decisions/1"
DECISIONS = ("allow", "deny", "ask")
SOURCES = ("oracle", "parity", "regression", "control", "recorded")
KINDS = ("command", "read", "write", "search")
_FIELDS = {"id", "payload", "expect", "source", "why", "known", "runner", "guards"}
Z95 = 1.959964

_ID = re.compile(r"[A-Za-z0-9._-]+")
_PLACEHOLDER = re.compile(r"(?<!\$)\{(HOME|CWD)\}")

# Each runner's hook payload for a neutral call: kind -> a function of (value, data, cwd). Adding a runner means one
# more entry here; a runner without one can still replay rows that carry their own payload.
_RENDER = {
    "claude-code": {
        "command": lambda v, d, cwd: {"tool_name": "Bash", "tool_input": {"command": v}},
        "read": lambda v, d, cwd: {"tool_name": "Read", "tool_input": {"file_path": v}},
        "write": lambda v, d, cwd: {
            "tool_name": "Write",
            "tool_input": {"file_path": v, "content": d.get("content", "")},
        },
        "search": lambda v, d, cwd: {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": v}},
    },
    "opencode": {
        "command": lambda v, d, cwd: {"tool": "bash", "args": {"command": v}},
        "read": lambda v, d, cwd: {"tool": "read", "args": {"filePath": v}},
        "write": lambda v, d, cwd: {"tool": "write", "args": {"filePath": v, "content": d.get("content", "")}},
        "search": lambda v, d, cwd: {"tool": "grep", "args": {"pattern": "x", "path": v}},
    },
}


@dataclass(frozen=True)
class Row:
    id: str
    payload: dict
    expect: str
    source: str
    why: str
    known: bool = False
    runner: str | None = None
    guards: tuple[str, ...] = field(default_factory=tuple)


# --- reading ----------------------------------------------------------------------------------------------------


def is_corpus(text: str) -> bool:
    """True when the first line that holds anything is a corpus row (an object with an `id` and a `source`)."""
    for line in text.splitlines():
        if line.strip():
            try:
                obj = json.loads(line)
            except ValueError:
                return False
            return isinstance(obj, dict) and "id" in obj and "source" in obj
    return False


def parse(text: str, name: str, seen: set[str] | None = None) -> list[Row]:
    """The rows of one file's text. `name` is what errors call the file; `seen` carries ids across files."""
    from ws import guards  # here, not at the top: the guards import the policy, which a corpus run loads anyway

    seen = set() if seen is None else seen
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        where = f"{name}:{n}"
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise WsError(
                "bad_input", f"{where} is not JSON ({e.__class__.__name__})", "one JSON object per line"
            ) from e
        if not isinstance(obj, dict):
            raise WsError("bad_input", f"{where} is not a JSON object", "one JSON object per line")
        if extra := sorted(set(obj) - _FIELDS):
            raise WsError(
                "bad_input", f"{where}: unknown key {extra[0]!r}", f"the keys are {', '.join(sorted(_FIELDS))}"
            )
        for key in ("id", "why"):
            if not isinstance(obj.get(key), str) or not obj[key].strip():
                raise WsError("bad_input", f"{where}: {key} must be non-empty text", "every row needs an id and a why")
        if not _ID.fullmatch(obj["id"]):
            raise WsError(
                "bad_input", f"{where}: id {obj['id']!r} has a character outside A-Z a-z 0-9 . _ -", "fix the id"
            )
        if obj["id"] in seen:
            raise WsError("bad_input", f"{where}: id {obj['id']!r} is used twice", "ids are unique across the corpus")
        seen.add(obj["id"])
        if obj.get("expect") not in DECISIONS:
            raise WsError("bad_input", f"{where}: expect must be one of {', '.join(DECISIONS)}", "fix the row")
        if obj.get("source") not in SOURCES:
            raise WsError("bad_input", f"{where}: source must be one of {', '.join(SOURCES)}", "fix the row")
        if not isinstance(obj.get("payload"), dict):
            raise WsError("bad_input", f"{where}: payload must be an object", "a neutral call or a runner's payload")
        runner = obj.get("runner")
        if runner is not None and not isinstance(runner, str):
            raise WsError("bad_input", f"{where}: runner must be text", "name the runner the payload is for")
        if runner is None:
            p = obj["payload"]
            if p.get("kind") not in KINDS or not isinstance(p.get("value"), str) or not p["value"]:
                raise WsError(
                    "bad_input",
                    f"{where}: a neutral payload needs a kind ({', '.join(KINDS)}) and a value",
                    "or give the runner's own payload with a runner",
                )
        known = obj.get("known", False)
        if not isinstance(known, bool):
            raise WsError("bad_input", f"{where}: known must be true or false", "fix the row")
        on = obj.get("guards", [])
        if not isinstance(on, list) or not all(isinstance(g, str) for g in on):
            raise WsError("bad_input", f"{where}: guards must be a list of names", "fix the row")
        if bad := [g for g in on if g not in guards.names()]:
            raise WsError("bad_input", f"{where}: unknown guard {bad[0]!r}", f"known: {', '.join(guards.names())}")
        rows.append(Row(obj["id"], obj["payload"], obj["expect"], obj["source"], obj["why"], known, runner, tuple(on)))
    return rows


def load(paths: list[Path]) -> list[Row]:
    """The rows of each file in turn. An unreadable file or a bad row is a typed error naming the file and line."""
    rows: list[Row] = []
    seen: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise WsError("not_found", f"cannot read {path} ({type(e).__name__})", "give a file of corpus rows") from e
        rows += parse(text, path.name, seen)
    if not rows:
        raise WsError("bad_input", "the corpus has no rows", "one JSON object per line")
    return rows


def fill(value, home: str, cwd: str):
    """`value` with `{HOME}` and `{CWD}` filled in, through nested lists and objects."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: home if m.group(1) == "HOME" else cwd, value)
    if isinstance(value, list):
        return [fill(v, home, cwd) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, home, cwd) for k, v in value.items()}
    return value


def render(row: Row, runner: str, home: str, cwd: str) -> dict | None:
    """The hook payload `runner` would send for the row, placeholders filled; None when the row is another runner's."""
    if row.runner is not None:
        return fill(row.payload, home, cwd) if row.runner == runner else None
    table = _RENDER.get(runner)
    if table is None:
        raise WsError(
            "bad_input",
            f"ws has no way to write a neutral call as a {runner} payload",
            "rows that carry their own payload and a runner replay on any runner",
        )
    p = fill(row.payload, home, cwd)
    data = {k: v for k, v in p.items() if k not in ("kind", "value")}
    out = table[p["kind"]](p["value"], data, cwd)
    if runner == "claude-code":
        out = {**out, "cwd": cwd, "session_id": "replay"}
    return out


# --- the numbers ------------------------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval for k out of n; None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k: int, n: int) -> dict:
    """{k, n, rate, ci}: a share with its Wilson 95% interval (rate and ci are None when n is 0)."""
    ci = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "ci": list(ci) if ci else None}


def confusion(rows: list[dict]) -> dict[str, dict[str, int]]:
    """expected -> judged -> count, over rows that carry `expect` and `result`."""
    m = {e: dict.fromkeys(DECISIONS, 0) for e in DECISIONS}
    for r in rows:
        m[r["expect"]][r["result"]] += 1
    return m


def _errors(m: dict[str, dict[str, int]]) -> dict[str, dict]:
    stop = sum(m["deny"].values()) + sum(m["ask"].values())
    return {
        "false_block": rate(m["allow"]["deny"] + m["allow"]["ask"], sum(m["allow"].values())),
        "miss": rate(m["deny"]["allow"] + m["ask"]["allow"], stop),
        "softened": rate(m["deny"]["ask"], sum(m["deny"].values())),
    }


def metrics(rows: list[dict]) -> dict:
    """The report: the confusion matrix, false-block, miss and softened rates, each source's row, and check() latency.
    A row is {id, source, expect, result, known, differs, ms}."""
    m = confusion(rows)
    by_source = {}
    for s in sorted({r["source"] for r in rows}):
        mine = [r for r in rows if r["source"] == s]
        e = _errors(confusion(mine))
        by_source[s] = {
            "rows": len(mine),
            "differ": sum(r["differs"] for r in mine),
            "known": sum(r["known"] for r in mine),
            "false_block": e["false_block"],
            "miss": e["miss"],
        }
    ms = [r["ms"] for r in rows]
    return {
        "rows": len(rows),
        "confusion": m,
        **_errors(m),
        "known": sum(r["known"] for r in rows),
        "by_source": by_source,
        "latency_ms": {"n": len(ms), "p50": percentile(ms, 50), "p95": percentile(ms, 95)},
    }


def failures(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(rows that differ and are not known, known rows that now pass): either list makes a run exit 1."""
    return (
        [r for r in rows if r["differs"] and not r["known"]],
        [r for r in rows if r["known"] and not r["differs"]],
    )


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _line(label: str, r: dict) -> str:
    ci = f"95% CI {_pct(r['ci'][0])} to {_pct(r['ci'][1])}" if r["ci"] else "no rows"
    return f"{label:<13}{r['k']:>4} of {r['n']:<4} {_pct(r['rate']):>6}  ({ci})"


def report_lines(mt: dict) -> list[str]:
    """The metrics as text: the matrix, the three rates, one row for each source, and the latency."""
    m = mt["confusion"]
    out = [f"{mt['rows']} rows ({mt['known']} known)", "", "confusion (rows: expected; columns: judged)"]
    out.append(f"{'':<8}" + "".join(f"{d:>7}" for d in DECISIONS))
    out += [f"{e:<8}" + "".join(f"{m[e][d]:>7}" for d in DECISIONS) for e in DECISIONS]
    out += ["", _line("false blocks", mt["false_block"]), _line("misses", mt["miss"])]
    out.append(_line("softened", mt["softened"]) + "  (deny came back as ask)")
    out += ["", f"{'source':<11}{'rows':>6}{'differ':>8}{'known':>7}{'false blocks':>14}{'misses':>9}"]
    for s, v in mt["by_source"].items():
        fb, mi = v["false_block"], v["miss"]
        out.append(
            f"{s:<11}{v['rows']:>6}{v['differ']:>8}{v['known']:>7}"
            f"{str(fb['k']) + '/' + str(fb['n']):>14}{str(mi['k']) + '/' + str(mi['n']):>9}"
        )
    lat = mt["latency_ms"]
    if lat["p50"] is not None:
        out += ["", f"check() latency: p50 {lat['p50']:.2f} ms, p95 {lat['p95']:.2f} ms over {lat['n']} rows"]
    return out


# --- saved decisions and the diff -------------------------------------------------------------------------------


def snapshot(runner: str, rows: list[dict]) -> dict:
    """What `--save` writes: each id's decision and the kind of layer that made it (`none` for an allow)."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "runner": runner,
        "decisions": {r["id"]: {"decision": r["result"], "kind": r["kind"] or "none"} for r in rows},
    }


def read_snapshot(path: Path, runner: str) -> dict[str, dict]:
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise WsError("not_found", f"cannot read {path} ({type(e).__name__})", "give a file `--save` wrote") from e
    if not isinstance(old, dict) or old.get("schema") != SNAPSHOT_SCHEMA or not isinstance(old.get("decisions"), dict):
        raise WsError("bad_input", f"{path} is not a saved set of decisions", "give a file `--save` wrote")
    if old.get("runner") != runner:
        raise WsError("bad_input", f"{path} was saved for {old.get('runner')}, not {runner}", "compare like with like")
    return old["decisions"]


def diff(old: dict[str, dict], rows: list[dict]) -> dict:
    """{changed, added, removed}: each id whose decision or layer kind differs, and the ids in only one side."""
    now = {r["id"]: {"decision": r["result"], "kind": r["kind"] or "none"} for r in rows}
    changed = [{"id": i, "from": old[i], "to": now[i]} for i in now if i in old and old[i] != now[i]]
    return {"changed": changed, "added": sorted(set(now) - set(old)), "removed": sorted(set(old) - set(now))}


def change_text(c: dict) -> str:
    a, b = c["from"], c["to"]
    if a["decision"] != b["decision"]:
        return f"{a['decision']} -> {b['decision']}"
    return f"{a['kind']} -> {b['kind']}"


def read_expected(path: Path) -> dict[str, str | None]:
    """The changes a person expects: `ID` (any change to it) or `ID: OLD -> NEW` (OLD and NEW the decisions, or for
    a change of layer with the same decision the kinds), one a line; `#` starts a comment."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WsError("not_found", f"cannot read {path} ({type(e).__name__})", "give a file of expected changes") from e
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, arrow = line.partition(":")
            out[name.strip()] = " ".join(arrow.split()) or None
    return out


def unexpected(changes: dict, expected: dict[str, str | None]) -> tuple[list[dict], list[str]]:
    """(the changed ids no line lists or whose arrow differs, the listed ids that did not change)."""
    unlisted = [
        c for c in changes["changed"] if c["id"] not in expected or expected[c["id"]] not in (None, change_text(c))
    ]
    moved = {c["id"] for c in changes["changed"]}
    return unlisted, sorted(i for i in expected if i not in moved)
