---
status: accepted
date: 2026-10-31
decision-makers: maintainer
---

# Build in public, runner-neutral, with Claude Code and OpenCode first

## Context and Problem Statement

ws should help anyone who works with AI coding tools, not one person's setup. It could be built privately and opened
later, or in public from the start. It could also be written for one tool and widened later, or kept neutral from the
first commit.

## Considered Options

- Private first, public once it works.
- Public from the first commit, with one vendor-neutral policy and a small first set of runners.

## Decision Outcome

Chosen option: public from the first commit, with one vendor-neutral `policy.toml` that each runner's adapter turns into
that runner's own config. The first two adapters are Claude Code and OpenCode. A further runner is a new module in
`src/ws/adapters/`; nothing else changes. Adapters for other runners wait for someone who uses them.

### Consequences

- Good: the design is tested against real use, and the policy never names one vendor.
- Good: adding a runner touches one folder.
- Bad: public wording must match the runners that exist. A claim about a runner with no adapter is wrong the day it's
  written (see [0016](0016-descriptions-say-what-ws-does-today.md)).

### Confirmation

`tests/test_install.py::test_the_gate_files_come_from_the_generator_for_each_runner` walks every adapter the folder
holds. `tests/test_principles.py::test_src_ws_imports_only_the_standard_library` keeps the core free of vendor SDKs.
