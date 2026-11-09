# Conformance

The runner conformance kit: the same seven questions asked of every runner adapter, so "works on Claude Code" and
"works on OpenCode" mean the same thing, and a new runner is held to the same bar the day its adapter exists. The suite
is `tests/conformance/`; the contracts and fixture rows are `src/ws/conformance.py`. CI runs it as the `conformance` job
in `cli.yml` on Claude Code and OpenCode, with stub models and no paid provider. It's not a required check: making it
one is a repository setting, and with that workflow's `paths:` filter a required check would wait forever on a
docs-only pull request.

## The rows

One table per runner found by `ws.adapters.all_adapters()`. A row is a test; its id is stable. Which rows drive a real
runner: only the live gate test (`live-gate`, below). The seven rows run in-process: they call ws's own code with the
adapter's generated config and hook payloads, and none of them starts a runner.

| Row | What it asks of the runner |
|---|---|
| `gate-parity` | The same neutral calls (a command, a read, a search, a write; denied, allowed and asked) get the same decision from the gate, and the same deciding layer (`want_rule`: `rule:command`, `rule:read`, `rule:search`, `rule:write`, `guard:NAME`, `ask_commands`). Each decision is also written as a `ws.gate-decision/1` record and checked. |
| `sandbox-holds` | The generated config, read as data (every JSON entry, and the argument list of any plugin), names ws's own files (the policy, the profile, the generated folder) and every `deny_read` path as whole entries, runs `ws gate check --runner NAME` with the policy and the protected folder, and names only guards that exist in `UNSUPPORTED_GUARDS`. The gate itself refuses a write to a protected path. |
| `budget-refuses` | A job over the monthly cap is refused with exit 3 and a `refused` record whose `stop_reason` is the cap's own (`refused:profile_cap`, and the error names the cap), before any runner starts or any lease is asked for. |
| `run-record` | A refused run still leaves exactly one record, valid against the run-record schema, naming the runner, and it maps onto `ws.run-event/1` events and the pinned telemetry attributes. |
| `skill-loads` | A `SKILL.md` in the runner's skills folder is valid Agent Skills (name equals folder, kebab case, description under 1024 characters, allowed keys only, under 20,000 bytes), every skill ws ships is too, the runner's own skill call is read as a skill call, and reading a skill file isn't stopped by the gate. |
| `mcp-tool-call` | `ws mcp serve` lists all its tools on the very first request, before any handshake (a runner that connects its MCP servers in the background can start a turn with none of the tools, and the model then answers without them), in a stable order, with valid names; then the handshake, a working tool call and a failing one that says so in its result. |
| `message-round-trip` | A message sent, listed, read and acknowledged between two sessions through the message store leaves the golden trace (`MSG_TRACE`, compared as sorted JSON lines; `ws mcp serve`'s message tools are listed by the MCP row, not driven), and a second message reaches the model at the runner's own prompt event, marked as untrusted data. |

`live-gate` is the live tier and the only test that drives a real runner: the real binary, pointed at a stub model on
127.0.0.1, with only ws's hook able to refuse (the native deny list or permission block is stripped). A denied command
must be blocked and the control call must run. It uses the drivers of `tests/e2e/test_gate_runners.py`, skips when the
runner isn't installed, and fails instead when `WS_E2E_REQUIRE=1`, as CI sets it. The run record of a real run, with a
broker lease, is `tests/e2e/test_run_record.py`; the `run-record` row above checks a refused run's record only.

The MCP project's own conformance suite isn't run: its server mode needs an HTTP server and node, and `ws mcp serve`
speaks stdio. The stdio checks above (tools present on the first call, deterministic order, name format, errors in the
result) cover the same ground for ws's server.

## What a runner is expected to do

Nothing is listed per runner in the suite. The expectations come from the adapter module, with a default derived from
what the adapter already has. An adapter may define an optional `CONFORMANCE` dict; every key is optional.

| Key | Meaning | Default |
|---|---|---|
| `payloads` | Call kind to a function `(value, data)` giving the runner's whole hook payload for it | the payload `ws.gatecorpus` writes for the runner |
| `skill_payload` | A function from a skill name to the hook payload of a skill call | the first common payload shape that the adapter's own `parse` reads as a skill call |
| `model` | The model a job names for this runner | `anthropic/claude-haiku-4-5` |
| `known_failures` | Row ids this runner fails today | none |
| `unsupported` | Row id to the reason this runner can't be asked that row | none |

Row ids are the seven above and `live-gate`.

### Known failures only shrink, and no row skips by default

A row a runner is known to fail is listed in `known_failures`. A listed row that fails is reported `INFO`, so a gap doesn't
block other work. A listed row that passes is a `FAILURE` ("stale"): take it off the list. A row that's not listed
and fails is a `FAILURE`. So the list can shrink and can't quietly grow without a person writing the id.

A row that can't be asked at all (no payload, no skill payload, no prompt event, no live driver) is a `FAILURE`, not a
skip: a runner doesn't go green by not being asked. Only a row the runner declares in `unsupported`, with a reason,
skips, and the skip says why. A declared row that passes is stale and fails, as above. The table also fails if
`ws.adapters` finds fewer than two runners, so an empty table can't pass.

A result is a record `{id, name, status, details, error}` with `status` one of `SUCCESS`, `FAILURE`, `WARNING`,
`SKIPPED` or `INFO`.

## The two contracts

Both are JSON Schema (draft 2020-12) as Python dicts, and use only the keywords the shipped checker (`ws.canary`)
reads.

`ws.gate-decision/1` is the portable policy-gate contract: a tool call in, `allow`, `deny` or `ask` out, the same for
every runner, hosted tool and subagent.

| Field | Meaning |
|---|---|
| `schema` | `ws.gate-decision/1` |
| `runner`, `tool`, `kind` | who asked, the tool's name and the neutral kind (`command`, `read`, `search`, `write`, `tool`, `agent`, `skill`, `message`) |
| `decision` | `allow`, `deny` or `ask` |
| `rule` | the layer that decided; null for an allow |
| `reason`, `hint` | why, and what to do instead; the reason is null only for an allow, and a hint is null when there is none |

`ws.run-event/1` is the common event a run emits, whichever runner ran it.

| Field | Meaning |
|---|---|
| `schema`, `run_id`, `seq`, `ts`, `runner` | the contract, the run, the position in it, when (UTC), who |
| `kind` | `run.start`, `model.call`, `tool.call`, `gate.decision` or `run.end` |
| `name`, `model` | the job or tool, and the model, or null |
| `input_tokens`, `output_tokens`, `duration_ms` | counts, or null when the runner can't say |
| `status`, `error` | `ok`, `error` or `refused`, and a short reason or null |

An event carries ids, names, counts and times, never a prompt, an argument or a result.

### Telemetry attributes

An event maps onto the OpenTelemetry GenAI attributes. Those conventions are in Development status, so the names ws
sends are pinned in `ws.conformance.OTEL` and in `tests/test_conformance_schema.py`: a rename upstream is an edit made on
purpose, never a silent change to what an exporter sends.

| Event field | Attribute |
|---|---|
| operation (`run.start` and `run.end` are `invoke_agent`, `model.call` is `chat`, `tool.call` and `gate.decision` are `execute_tool`) | `gen_ai.operation.name` |
| `runner` | `gen_ai.agent.name` |
| `run_id` | `gen_ai.conversation.id` |
| `model` | `gen_ai.request.model` |
| `input_tokens`, `output_tokens` | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `name` of a tool event | `gen_ai.tool.name` |
| `error` of an event that's not `ok` | `error.type` |

The span name is the operation and its target: `execute_tool Bash`, `chat claude-haiku-4-5`, `invoke_agent opencode`.

## Adding a runner

Add the adapter module; the table appears. If the runner's hook payloads aren't ones `ws.gatecorpus` can write, give
`CONFORMANCE["payloads"]`. For the live tier, add a driver to `LIVE` in `tests/conformance/test_runner_table.py` and the
runner to the `conformance` job. Rows the runner fails at first go in `known_failures`, each with an issue.
