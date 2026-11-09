"""The runner conformance kit: what every runner adapter must do the same way, as data and a few small helpers.

The suite is `tests/conformance/`: one table per runner found by `ws.adapters.all_adapters()`, so a new adapter is
covered the day its module is added. The rows are `ROWS`: gate parity, sandbox holds, budget refuses, run record
written, a skill loads, an MCP tool call, a message round trip. This module holds what the rows share: the two
contracts they check (`GATE_DECISION` and `RUN_EVENT`, JSON Schema as Python dicts, with `OTEL` pinning how a run
event maps onto the GenAI telemetry attributes), the fixture rows, and the stale-aware known-failure rule.

What a runner is expected to do comes from the adapter, never from a list here. An adapter may define an optional
`CONFORMANCE` dict; every key has a default derived from what the adapter already has:

    payloads: dict            Call kind -> function(value, data) giving that runner's whole hook payload for it;
                              default: the one `ws.gatecorpus` writes for the runner
    skill_payload: callable   name -> the hook payload for a skill call; default: the first of the payload shapes
                              in `SKILL_SHAPES` that the adapter's own `parse` reads as a skill call
    model: str                the model a job names for this runner; default `anthropic/claude-haiku-4-5`, a form
                              every ws runner accepts in a job that is refused before the runner starts
    known_failures: tuple     row ids this runner is known to fail today; see `settle`
    unsupported: dict         row id -> the reason this runner cannot be asked that row

A row that cannot be asked of a runner (no payload, no skill payload, no prompt event) is a failure, never a skip,
unless the runner declares it in `unsupported` with a reason. Only then does the row skip, and the skip says why. A row
that fails and is listed in `known_failures` is reported INFO; a row that passes while listed there or in `unsupported`
fails ("stale"), so both lists only ever shrink. Result records follow the check shape `{id, name, status, details,
error}`.

Nothing here runs a model or reads the network: the live tier of the suite uses the stub models in `tests/e2e`.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import ModuleType

from ws import adapters, canary
from ws.output import WsError

GATE_DECISION_ID = "ws.gate-decision/1"
RUN_EVENT_ID = "ws.run-event/1"
DECISIONS = ("allow", "deny", "ask")
EVENT_KINDS = ("run.start", "model.call", "tool.call", "gate.decision", "run.end")
STATUSES = ("SUCCESS", "FAILURE", "WARNING", "SKIPPED", "INFO")

LIVE_ROW = "live-gate"  # the one test that drives a real runner; it takes part in `known_failures` and `unsupported`
ROWS = (
    "gate-parity",
    "sandbox-holds",
    "budget-refuses",
    "run-record",
    "skill-loads",
    "mcp-tool-call",
    "message-round-trip",
)

# The portable policy-gate contract: a tool call and its arguments in, allow / deny / ask out, with the reason and a
# hint when there is one. The same for every runner, hosted tool and subagent. `rule` is the layer that decided
# (`rule:KIND`, `guard:NAME`, `ask_commands`, `preflight` or `error`), null for an allow.
_NULL_TEXT = {"type": ["string", "null"]}
GATE_DECISION: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:ws:schema:gate-decision:1",
    "title": "ws gate decision",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "runner", "tool", "kind", "decision", "rule", "reason", "hint"],
    "properties": {
        "schema": {"const": GATE_DECISION_ID},
        "runner": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "minLength": 1},
        "decision": {"enum": list(DECISIONS)},
        "rule": dict(_NULL_TEXT),
        "reason": dict(_NULL_TEXT),
        "hint": dict(_NULL_TEXT),
    },
}

_COUNT = {"type": ["integer", "null"], "minimum": 0}
RUN_EVENT: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:ws:schema:run-event:1",
    "title": "ws run event",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "run_id",
        "seq",
        "ts",
        "runner",
        "kind",
        "name",
        "model",
        "input_tokens",
        "output_tokens",
        "duration_ms",
        "status",
        "error",
    ],
    "properties": {
        "schema": {"const": RUN_EVENT_ID},
        "run_id": {"type": "string", "minLength": 1},
        "seq": {"type": "integer", "minimum": 0},
        "ts": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*Z$"},
        "runner": {"type": "string", "minLength": 1},
        "kind": {"enum": list(EVENT_KINDS)},
        "name": dict(_NULL_TEXT),
        "model": dict(_NULL_TEXT),
        "input_tokens": dict(_COUNT),
        "output_tokens": dict(_COUNT),
        "duration_ms": dict(_COUNT),
        "status": {"enum": ["ok", "error", "refused"]},
        "error": dict(_NULL_TEXT),
    },
}

# How a run event maps onto the OpenTelemetry GenAI conventions. Those conventions are still in Development status, so
# the attribute names ws emits are pinned here, as strings, and a test fails when the table and `to_otel` disagree. A
# rename upstream is a deliberate edit of this table, never a silent change of what an exporter sends. Content (prompts,
# tool arguments, results) has no attribute: an exported event carries ids, names, counts and times only.
OTEL: dict = {
    "status": "Development",
    "operation": {  # run event kind -> gen_ai.operation.name; span name = "<operation> <target>"
        "run.start": "invoke_agent",
        "model.call": "chat",
        "tool.call": "execute_tool",
        "gate.decision": "execute_tool",
        "run.end": "invoke_agent",
    },
    "attributes": {  # run event field -> attribute name
        "operation": "gen_ai.operation.name",
        "runner": "gen_ai.agent.name",
        "run_id": "gen_ai.conversation.id",
        "model": "gen_ai.request.model",
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
        "tool": "gen_ai.tool.name",
        "error": "error.type",
    },
}


def problems(instance: dict, schema: dict) -> list[str]:
    """What is wrong with `instance` against `schema` (the keywords `ws.canary.validate` reads); empty means valid."""
    return canary.validate(instance, schema)


def decision(
    runner: str, tool: str, kind: str, result: str, rule: str | None = None, reason: str | None = None,
    hint: str | None = None,
) -> dict:  # fmt: skip
    """One `ws.gate-decision/1` record. An allow carries no rule and no reason; a deny or an ask must carry both."""
    rec = {
        "schema": GATE_DECISION_ID, "runner": runner, "tool": tool, "kind": kind, "decision": result,
        "rule": rule, "reason": reason, "hint": hint,
    }  # fmt: skip
    if bad := problems(rec, GATE_DECISION):
        raise ValueError("; ".join(bad))
    if (result == "allow") != (rule is None and reason is None):
        raise ValueError("an allow has no rule or reason; a deny or ask has both")
    if result != "allow" and not (rule and reason):
        raise ValueError("a deny or ask has both a rule and a reason")
    return rec


def run_event(run_id: str, seq: int, ts: str, runner: str, kind: str, **fields) -> dict:
    """One `ws.run-event/1` record; fields not given are null (status defaults to ok)."""
    rec = {
        "schema": RUN_EVENT_ID, "run_id": run_id, "seq": seq, "ts": ts, "runner": runner, "kind": kind,
        "name": None, "model": None, "input_tokens": None, "output_tokens": None, "duration_ms": None,
        "status": "ok", "error": None,
    }  # fmt: skip
    unknown = set(fields) - set(rec)
    if unknown:
        raise KeyError(f"not a run event field: {', '.join(sorted(unknown))}")
    rec.update(fields)
    if bad := problems(rec, RUN_EVENT):
        raise ValueError("; ".join(bad))
    return rec


def to_otel(event: dict) -> dict:
    """A run event as a span description: `{name, attributes}` using only the names pinned in `OTEL`. Null fields are
    left out. A tool event names its tool as the span's target; the others name the model, else the runner."""
    attrs = OTEL["attributes"]
    op = OTEL["operation"][event["kind"]]
    tool = event["name"] if event["kind"] in ("tool.call", "gate.decision") else None
    target = tool or event["model"] or event["runner"]
    values = {
        "operation": op, "runner": event["runner"], "run_id": event["run_id"], "model": event["model"],
        "input_tokens": event["input_tokens"], "output_tokens": event["output_tokens"], "tool": tool,
        "error": event["error"] if event["status"] != "ok" else None,
    }  # fmt: skip
    return {"name": f"{op} {target}", "attributes": {attrs[k]: v for k, v in values.items() if v is not None}}


def event_from_gate(rec: dict, run_id: str, seq: int, ts: str) -> dict:
    """The run event for a gate decision, so the two contracts meet: tool name, a deny's reason as the error."""
    denied = rec["decision"] != "allow"
    return run_event(
        run_id, seq, ts, rec["runner"], "gate.decision", name=rec["tool"],
        status="refused" if denied else "ok", error=rec["reason"] if denied else None,
    )  # fmt: skip


def event_from_record(rec: dict, seq: int = 0) -> list[dict]:
    """The start and end events of a finished run record (`ws.runschema`): what a `runs.jsonl` line says, as events."""
    model = rec.get("model_requested")
    status = {"ok": "ok", "refused": "refused"}.get(rec.get("status"), "error")
    tokens = rec.get("tokens") or {}
    start = run_event(
        rec["run_id"], seq, rec["started_at"], rec["runner"], "run.start", name=rec.get("job"), model=model
    )
    end = run_event(
        rec["run_id"], seq + 1, rec["ended_at"] or rec["started_at"], rec["runner"], "run.end", name=rec.get("job"),
        model=model, input_tokens=tokens.get("input"), output_tokens=tokens.get("output"),
        duration_ms=rec.get("duration_ms"), status=status, error=rec.get("error"),
    )  # fmt: skip
    return [start, end]


# --- what an adapter is expected to do ---------------------------------------------------------------------------------


def _shape(tool: str, key: str, fields: tuple[str, str]) -> Callable[[str], dict]:
    return lambda name: {fields[0]: tool, fields[1]: {key: name}}


# The hook payload shapes runners send for a tool call: a tool name and its arguments under one of two spellings. The
# default skill payload is the first of these that the adapter's own `parse` reads as a skill call.
SKILL_SHAPES: tuple[Callable[[str], dict], ...] = tuple(
    _shape(tool, key, fields)
    for tool in ("Skill", "skill")
    for key in ("skill", "name")
    for fields in (("tool_name", "tool_input"), ("tool", "args"))
)


class Unsupported(Exception):
    """Raised by a row that cannot be asked of this runner; `settle` turns it into a failure unless the runner declares
    the row in `unsupported`, then into a skip that names the reason."""


def derived_skill_payload(mod: ModuleType) -> Callable[[str], dict] | None:
    """The first of `SKILL_SHAPES` the adapter's `parse` reads as a skill call, else None."""
    if not callable(getattr(mod, "parse", None)):
        return None
    for shape in SKILL_SHAPES:
        try:
            got = mod.parse(shape("probe"))
        except (ValueError, TypeError, KeyError):
            continue
        calls = [got] if isinstance(got, adapters.Call) else got or []
        if [(c.kind, c.value) for c in calls] == [("skill", "probe")]:
            return shape
    return None


MODEL = "anthropic/claude-haiku-4-5"


@dataclass(frozen=True)
class Expect:
    """What one runner is expected to do, derived from its adapter and its optional `CONFORMANCE` dict."""

    runner: str
    mod: ModuleType
    payloads: Mapping[str, Callable] = field(default_factory=dict)  # Call kind -> function(value, data) -> payload
    skill_payload: Callable[[str], dict] | None = None
    model: str = MODEL
    known_failures: tuple[str, ...] = ()
    unsupported: Mapping[str, str] = field(default_factory=dict)  # row id -> why this runner cannot be asked it


def expectations(mod: ModuleType) -> Expect:
    """The `Expect` for an adapter module: its `CONFORMANCE` where it gives a key, else the derived default."""
    conf = getattr(mod, "CONFORMANCE", None) or {}
    if not isinstance(conf, dict):
        raise TypeError(f"{mod.__name__}.CONFORMANCE must be a dict")
    unknown = set(conf) - {"payloads", "skill_payload", "model", "known_failures", "unsupported"}
    if unknown:
        raise KeyError(f"{mod.__name__}.CONFORMANCE: unknown key {min(unknown)!r}")
    known = tuple(conf.get("known_failures", ()))
    if bad := [k for k in known if k not in (*ROWS, LIVE_ROW)]:
        raise KeyError(f"{mod.__name__}.CONFORMANCE: known_failures names no row: {bad[0]!r}")
    unsupported = dict(conf.get("unsupported") or {})
    if bad := [
        k for k, why in unsupported.items() if k not in (*ROWS, LIVE_ROW) or not (isinstance(why, str) and why.strip())
    ]:
        raise KeyError(f"{mod.__name__}.CONFORMANCE: unsupported needs a row id and a reason: {bad[0]!r}")
    return Expect(
        mod.NAME, mod, conf.get("payloads") or {}, conf.get("skill_payload") or derived_skill_payload(mod),
        conf.get("model") or MODEL, known, unsupported,
    )  # fmt: skip


def all_expectations() -> dict[str, Expect]:
    """One `Expect` per adapter `ws.adapters` finds."""
    return {name: expectations(mod) for name, mod in adapters.all_adapters().items()}


def payload_for(exp: Expect, kind: str, value: str, data: Mapping | None, home: str, cwd: str) -> dict | None:
    """The hook payload `exp.runner` sends for a neutral call, or None when ws has no way to write one: the adapter's own
    `payloads` first, else the corpus renderer."""
    data = dict(data or {})
    own = exp.payloads.get(kind)
    if own is not None:
        return own(value, data)
    from ws import gatecorpus

    row = gatecorpus.Row("conformance", {"kind": kind, "value": value, **data}, "allow", "control", "row")
    try:
        return gatecorpus.render(row, exp.runner, home, cwd)
    except WsError:
        return None


# --- the gate fixture rows ---------------------------------------------------------------------------------------------
# One neutral call each, what a person would want, and the layer that should decide (`want_rule`; None for an allow).
# `{HOME}` and `{CWD}` are the scratch folders the suite makes. The policy is the built-in one, except `ask_commands`
# rows, which name the policy text they need in `policy`.

ASK_POLICY = '[gate]\nask_commands = ["conformance-ask*"]\n'

GATE_ROWS: tuple[dict, ...] = (
    {"id": "deny-force-push", "kind": "command", "value": "git push --force origin main", "want": "deny",
     "want_rule": "rule:command"},
    {"id": "deny-private-key", "kind": "read", "value": "{HOME}/.ssh/id_rsa", "want": "deny", "want_rule": "rule:read"},
    {"id": "deny-key-folder-search", "kind": "search", "value": "{HOME}/.ssh", "want": "deny",
     "want_rule": "rule:search"},
    {"id": "deny-own-policy-write", "kind": "write", "value": "{HOME}/.ws/policy.toml", "want": "deny",
     "want_rule": "rule:write"},
    {"id": "deny-compound-hides-push", "kind": "command", "value": "ls && git push --force origin main",
     "want": "deny", "want_rule": "rule:command"},
    {"id": "deny-cloud-credentials", "kind": "read", "value": "{HOME}/.aws/credentials", "want": "deny",
     "want_rule": "rule:read"},
    {"id": "deny-recursive-delete-of-home", "kind": "command", "value": "rm -rf ~", "want": "deny",
     "want_rule": "rule:command"},
    {"id": "allow-status", "kind": "command", "value": "git status", "want": "allow", "want_rule": None},
    {"id": "allow-read-in-project", "kind": "read", "value": "{CWD}/notes.txt", "want": "allow", "want_rule": None},
    {"id": "allow-write-in-project", "kind": "write", "value": "{CWD}/out.txt", "want": "allow", "want_rule": None},
    {"id": "ask-listed-command", "kind": "command", "value": "conformance-ask now", "want": "ask",
     "want_rule": "ask_commands", "policy": ASK_POLICY},
)  # fmt: skip


def fill(text: str, home: str, cwd: str) -> str:
    return text.replace("{HOME}", home).replace("{CWD}", cwd)


# --- a skill, as a runner reads it -------------------------------------------------------------------------------------

SKILL_NAME = "conformance-probe"
SKILL_TEXT = (
    f"---\nname: {SKILL_NAME}\ndescription: Use when a conformance run needs a skill to load. A probe, not a real skill.\n"
    "---\n\n# Conformance probe\n\nSay the word probe and stop.\n"
)
SKILL_MAX_BYTES = 20_000


def skill_problems(text: str, folder_name: str) -> list[str]:
    """What is wrong with a SKILL.md against the Agent Skills format: front matter that reads cleanly, a kebab-case `name`
    of at most 64 characters equal to its folder, a `description` of 1 to 1024 characters with no angle brackets, only
    the keys the format allows, and a file under SKILL_MAX_BYTES."""
    from ws import frontmatter

    out = []
    doc = frontmatter.parse(text)
    if doc.raw is None:
        return ["no front matter"]
    if not doc.clean:
        out.append("front matter is outside the safe subset")
    name, desc = doc.meta.get("name"), doc.meta.get("description")
    if not (isinstance(name, str) and name and len(name) <= 64 and all(c.isalnum() or c == "-" for c in name)):
        out.append("name must be 1-64 characters of a-z, 0-9 and -")
    elif name != name.lower() or name.startswith("-") or name.endswith("-") or "--" in name:
        out.append("name must be lower case with single inner hyphens")
    elif name != folder_name:
        out.append(f"name {name!r} is not the folder name {folder_name!r}")
    if not (isinstance(desc, str) and 1 <= len(desc) <= 1024) or "<" in str(desc) or ">" in str(desc):
        out.append("description must be 1-1024 characters, without < or >")
    if extra := sorted(
        set(doc.meta) - {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    ):
        out.append(f"key {extra[0]!r} is not part of the format")
    if len(text.encode("utf-8")) > SKILL_MAX_BYTES:
        out.append(f"larger than {SKILL_MAX_BYTES} bytes")
    return out


# --- a golden message round trip ---------------------------------------------------------------------------------------
# The steps a message takes between two sessions, as the facts each step must leave (never ids or times, which differ
# between runs). The trace is replayed through the message store (`ws.msg`) only; the `mcp-tool-call` row checks that `ws mcp serve`
# lists the message tools but does not call them.

MSG_TRACE: tuple[dict, ...] = (
    {"step": "send", "as": "alpha", "to": "bravo", "kind": "ask", "body": "is the build green?", "ack_required": True,
     "want": {"from": "alpha", "to": "bravo", "kind": "ask", "ack_required": True, "read": False}},
    {"step": "list", "as": "bravo", "want": {"count": 1, "unread": 1}},
    {"step": "read", "as": "bravo", "want": {"body": "is the build green?", "from": "alpha"}},
    {"step": "list", "as": "bravo", "unread": True, "want": {"count": 0}},
    {"step": "ack", "as": "bravo", "want": {"acked": True}},
)  # fmt: skip


def golden_lines(steps: list[dict]) -> str:
    """The facts a replay saw, one JSON line per step with sorted keys: what the golden trace compares."""
    return "".join(json.dumps(s, sort_keys=True) + "\n" for s in steps)


# --- known failures, stale-aware ---------------------------------------------------------------------------------------


def check(row: str, name: str, status: str, details: str = "", error: str | None = None) -> dict:
    """A result record in the shape of a conformance check: `{id, name, status, details, error}`."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    return {"id": row, "name": name, "status": status, "details": details, "error": error}


def settle(exp: Expect, row: str, error: BaseException | None) -> dict:
    """The result of one row for one runner. A pass is SUCCESS and a failure is FAILURE, with three exceptions. A row in
    `known_failures`: a failure is INFO and a pass is FAILURE (stale: take it off the list). A row in `unsupported`: a
    pass is FAILURE (stale) and anything else is SKIPPED with the declared reason. A row that raised `Unsupported`
    without being declared is FAILURE: a runner does not go green by not being asked."""
    name = f"{exp.runner}: {row}"
    if row in exp.unsupported:
        if error is None:
            return check(
                row, name, "FAILURE", error=f"{row} is declared unsupported for {exp.runner} but passes: remove it"
            )
        return check(row, name, "SKIPPED", f"unsupported: {exp.unsupported[row]}")
    known = row in exp.known_failures
    if isinstance(error, Unsupported):
        hint = f"declare CONFORMANCE['unsupported'][{row!r}] with a reason, or fix the adapter"
        return check(row, name, "FAILURE", error=f"{error}; {hint}")
    if error is None:
        if known:
            return check(
                row, name, "FAILURE", error=f"{row} is in known_failures of {exp.runner} but passes: remove it"
            )
        return check(row, name, "SUCCESS")
    why = f"{type(error).__name__}: {error}"[:300]
    return check(row, name, "INFO" if known else "FAILURE", "known failure" if known else "", why)


# --- a generated config, read as data ----------------------------------------------------------------------------------


def config_entries(files: Mapping[str, str]) -> tuple[set[str], list[list[str]]]:
    """What a runner's generated config says, read as data and not searched as text. Returns (entries, argvs): `entries`
    is every string of every JSON file (object keys and values) reduced by `rule_path`, so a path written as a rule
    compares equal to the path; `argvs` is every command the config runs: each JSON string split like a shell line, and
    the string literals of any other file (a plugin's argument list) in the order they appear."""
    entries: set[str] = set()
    argvs: list[list[str]] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            entries.add(rule_path(node))
            try:
                argvs.append(shlex.split(node))
            except ValueError:
                pass

    for name, text in files.items():
        if name.endswith(".json"):
            walk(json.loads(text))
        else:
            argvs.append([json.loads(f'"{m}"') for m in re.findall(r'"((?:[^"\\\n]|\\.)*)"', text)])
    return entries, argvs


def rule_path(rule: str) -> str:
    """The path a permission rule names: `Edit(//x/y/**)`, `/x/y/**` and `/x/y` are all `/x/y`."""
    m = re.fullmatch(r"[A-Za-z]+\((.*)\)", rule)
    path = m.group(1) if m else rule
    path = re.sub(r"^//", "/", path)
    return path.removesuffix("/**").removesuffix("/") or path


def runs_in_order(argvs: list[list[str]], *tokens: str) -> bool:
    """True when some command in `argvs` holds `tokens` next to each other, in that order."""
    n = len(tokens)
    return any(tuple(a[i : i + n]) == tokens for a in argvs for i in range(len(a) - n + 1))
