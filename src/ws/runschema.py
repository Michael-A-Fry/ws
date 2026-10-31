"""The JSON Schema (draft 2020-12) of one run record, as a Python dict: the shape of a line in `runs.jsonl`.

It is a dict and not a `.json` file because `ws secret wall` copies only `.py` files. `runrecord.schema()` hands out a
copy, and `ws canary` checks a sample record against it with a small checker that understands only these keywords:
`type`, `const`, `enum`, `pattern`, `minimum`, `minLength`, `maxLength`, `required`, `properties`,
`additionalProperties: false` and `items`. Keep to them. Every key is always present in a record; an unknown value is
`null`, never a missing key. Nothing here holds a secret, and the `error` field is short by rule.
"""

from __future__ import annotations

_STAMP = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*Z$"

_COUNT = {"type": ["integer", "null"], "minimum": 0}
_TEXT_OR_NULL = {"type": ["string", "null"]}

_MODEL_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "input", "output", "cache_creation", "cache_read", "cost_usd"],
    "properties": {
        "model": {"type": "string", "minLength": 1},
        "input": dict(_COUNT),
        "output": dict(_COUNT),
        "cache_creation": dict(_COUNT),
        "cache_read": dict(_COUNT),
        "cost_usd": {"type": ["number", "null"], "minimum": 0},
    },
}

_TOKENS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["input", "output", "cache_creation", "cache_read"],
    "properties": {
        "input": dict(_COUNT),
        "output": dict(_COUNT),
        "cache_creation": dict(_COUNT),
        "cache_read": dict(_COUNT),
    },
}

SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/Michael-A-Fry/ws/schemas/run-record.schema.json",
    "title": "ws run record",
    "description": (
        "One line of runs.jsonl: what one headless job did, whichever runner ran it. "
        "Unknown values are null, never missing."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
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
    ],
    "properties": {
        "schema": {
            "const": 2,
            "description": (
                "Record version. Version 2 added green and recovered; older lines in a log are not rewritten, so a "
                "reader must cope without them. This schema describes version 2 only."
            ),
        },
        "run_id": {
            "type": "string",
            "pattern": r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$",
            "description": "Sorts by time: the UTC start, then six random hex digits.",
        },
        "job": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$", "description": "The job file's name."},
        "runner": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "Which AI runner ran the job. Null only when the job file could not be read.",
        },
        "runner_version": {"type": ["string", "null"], "description": "The runner's own version string."},
        "exe_path": {"type": ["string", "null"], "description": "The runner executable, as found on PATH."},
        "model_requested": {"type": ["string", "null"], "description": "The model the job asked for."},
        "models_used": {
            "type": "array",
            "items": _MODEL_ITEM,
            "description": "One item per model the runner reported, or the requested model if it names none.",
        },
        "started_at": {"type": "string", "pattern": _STAMP, "description": "When the job started (UTC)."},
        "ended_at": {"type": "string", "pattern": _STAMP, "description": "When the job ended (UTC)."},
        "duration_ms": {"type": ["integer", "null"], "minimum": 0, "description": "Wall-clock time of the run."},
        "duration_api_ms": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Time the runner says it spent waiting on the model API.",
        },
        "exit_code": {
            "type": ["integer", "null"],
            "description": "The runner's exit code. Null if it never ran or was killed.",
        },
        "status": {"enum": ["ok", "error", "timeout", "refused"], "description": "How the run ended."},
        "error": {
            "type": ["string", "null"],
            "maxLength": 300,
            "description": "A short reason when the run did not go well. Never a secret or a lease URL.",
        },
        "session_id": {"type": ["string", "null"], "description": "The runner's session id, if it gave one."},
        "transcript_path": {
            "type": ["string", "null"],
            "description": "Where the runner kept the transcript: a file for one runner, a session database for another.",
        },
        "tokens": {**_TOKENS, "description": "Token counts for the whole run."},
        "cost_usd": {"type": ["number", "null"], "minimum": 0, "description": "What the run cost, in US dollars."},
        "cost_basis": {
            "type": ["string", "null"],
            "description": (
                "Where cost_usd came from: list (an estimate at list price), runner (the runner's own figure), "
                "mixed, or unpriced (tokens were counted but no price was known, so cost is null)."
            ),
        },
        "num_turns": {"type": ["integer", "null"], "minimum": 0, "description": "Model turns in the run."},
        "cwd": {"type": ["string", "null"], "description": "The folder the job ran in."},
        "policy": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dial", "rule_of_two"],
            "properties": {
                "dial": {"type": ["string", "null"], "description": "The autonomy dial the job ran under."},
                "rule_of_two": {
                    "type": ["string", "null"],
                    "pattern": r"^(ok|refused|exception:[A-Za-z0-9_-]+)$",
                    "description": "The rule-of-two outcome: ok, refused, or exception:<name>.",
                },
            },
            "description": "The policy the run was held to.",
        },
        "secrets_used": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "host"],
                "properties": {"name": {"type": "string"}, "host": {"type": "string"}},
            },
            "description": "Secrets the run was allowed to use: names and hosts only, never values.",
        },
        "ws_version": {"type": "string", "description": "The ws build that wrote the record."},
        "output_truncated": {
            "type": "boolean",
            "description": "True when the runner printed more than the output cap and the rest was dropped.",
        },
        "allow_unpriced": {
            "type": "boolean",
            "description": "True when the job allowed a run whose cost could not be known in advance.",
        },
        "green": {
            "enum": ["targeted", "package", "full", "merge_ready", None],
            "description": (
                "What the job's verification covers, as its job file declares: targeted (tests for what changed), "
                "package (the whole package), full (the whole suite), merge_ready (the whole suite plus review and "
                "lint gates). Null when the job says nothing."
            ),
        },
        "recovered": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "action", "ok"],
                "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "action": {"type": "string", "minLength": 1},
                    "ok": {"type": "boolean"},
                },
            },
            "description": "Automatic recoveries tried before or during the run, and whether each worked.",
        },
        "eval": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["task", "trial", "arm", "pass"],
            "properties": {
                "task": {"type": "string"},
                "trial": {"type": "integer", "minimum": 1},
                "arm": {"type": "string"},
                "pass": {"type": ["boolean", "null"]},
            },
            "description": "Set only on a trial of the eval command. pass is null when written: grading comes later.",
        },
    },
}
