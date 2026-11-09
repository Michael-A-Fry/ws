"""The two contracts the conformance kit tests, `ws.gate-decision/1` and `ws.run-event/1`: they are valid JSON Schema,
the small checker ws ships agrees with a full validator on them, the OpenTelemetry attribute names are pinned, and the
fixture rows are well formed. The runner table itself is in tests/conformance/."""

from __future__ import annotations

import jsonschema
import pytest

from ws import conformance, guards

GATE = conformance.decision("claude-code", "Bash", "command", "deny", "rule:command", "denied", "use git push")
EVENT = conformance.run_event("run-1", 0, "2026-01-01T00:00:00Z", "claude-code", "run.start", name="daily")


@pytest.mark.parametrize("schema", [conformance.GATE_DECISION, conformance.RUN_EVENT])
def test_the_schema_is_valid_draft_2020_12(schema):
    jsonschema.Draft202012Validator.check_schema(schema)


def test_the_ids_are_versioned_and_distinct():
    assert conformance.GATE_DECISION["properties"]["schema"]["const"] == "ws.gate-decision/1"
    assert conformance.RUN_EVENT["properties"]["schema"]["const"] == "ws.run-event/1"
    assert conformance.GATE_DECISION["$id"] != conformance.RUN_EVENT["$id"]


@pytest.mark.parametrize(
    ("record", "schema"),
    [(GATE, conformance.GATE_DECISION), (EVENT, conformance.RUN_EVENT)],
)
def test_the_shipped_checker_and_a_full_validator_agree(record, schema):
    """The checker ws uses at run time knows a few keywords; for these schemas it must say what a full one says."""
    validator = jsonschema.Draft202012Validator(schema)
    cases = [
        record,
        {**record, "schema": "ws.other/1"},
        {**record, "extra": 1},
        {k: v for k, v in record.items() if k != "runner"},
    ]
    cases += [{**record, "runner": ""}, {**record, "runner": 3}]
    for case in cases:
        full = not list(validator.iter_errors(case))
        assert (conformance.problems(case, schema) == []) == full, case


def test_a_run_event_needs_a_known_kind_status_and_stamp():
    for bad in ({"kind": "tool.start"}, {"status": "weird"}, {"ts": "yesterday"}, {"seq": -1}, {"input_tokens": -3}):
        assert conformance.problems({**EVENT, **bad}, conformance.RUN_EVENT), bad


def test_the_otel_attribute_names_are_pinned():
    """A rename of any of these is an edit of this test: what an exporter sends does not change by accident."""
    assert conformance.OTEL["status"] == "Development"
    assert conformance.OTEL["attributes"] == {
        "operation": "gen_ai.operation.name",
        "runner": "gen_ai.agent.name",
        "run_id": "gen_ai.conversation.id",
        "model": "gen_ai.request.model",
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
        "tool": "gen_ai.tool.name",
        "error": "error.type",
    }
    assert set(conformance.OTEL["operation"].values()) == {"invoke_agent", "chat", "execute_tool"}


def test_no_content_has_an_attribute():
    """Prompts, tool arguments and results are never exported: no attribute and no event field carries them."""
    content = ("prompt", "completion", "input_messages", "output_messages", "arguments", "result", "content")
    fields = set(conformance.RUN_EVENT["properties"])
    assert not fields & set(content)
    assert not any(
        any(c in a for c in ("prompt", "completion", "message", "arguments", "result"))
        for a in conformance.OTEL["attributes"].values()
    )


def test_the_gate_fixture_rows_are_well_formed():
    ids = [r["id"] for r in conformance.GATE_ROWS]
    assert len(ids) == len(set(ids)) >= 8
    for r in conformance.GATE_ROWS:
        assert r["kind"] in ("command", "read", "write", "search") and r["want"] in conformance.DECISIONS
        assert (r["want_rule"] is None) == (r["want"] == "allow"), r["id"]  # every refusal says which layer decided
        assert r["want_rule"] is None or r["want_rule"].split(":")[0] in ("rule", "guard", "ask_commands", "preflight")
        if r["want_rule"] and r["want_rule"].startswith("guard:"):
            assert r["want_rule"].split(":")[1] in guards.names()
    assert {r["want"] for r in conformance.GATE_ROWS} == set(conformance.DECISIONS)  # all three outcomes are covered


def test_the_row_list_matches_the_docs():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docs" / "reference" / "conformance.md").read_text(encoding="utf-8")
    for row in conformance.ROWS:
        assert f"`{row}`" in text, f"docs/reference/conformance.md does not describe the row {row}"
    assert "ws.gate-decision/1" in text and "ws.run-event/1" in text


def test_the_golden_message_trace_is_in_a_fixed_order():
    steps = [s["step"] for s in conformance.MSG_TRACE]
    assert steps == ["send", "list", "read", "list", "ack"]
    lines = conformance.golden_lines(list(conformance.MSG_TRACE)).splitlines()
    assert len(lines) == len(steps) and all(line.startswith("{") for line in lines)
