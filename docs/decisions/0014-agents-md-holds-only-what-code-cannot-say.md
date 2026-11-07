---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# Keep AGENTS.md to facts an agent can't discover, and the code map in the reference

## Context and Problem Statement

`AGENTS.md` is read at the start of every agent session, so every line costs context each time. A file that repeats what
the code, the tests or the docs already say makes agents slower and no more accurate. The generated code map was the
largest part of it, and an agent can find a module's purpose by reading its docstring.

## Considered Options

- Keep the code map in `AGENTS.md` and trim elsewhere.
- Keep in `AGENTS.md` only what can't be found by reading the repo, and move the generated map to `docs/reference/`.

## Decision Outcome

Chosen option: `AGENTS.md` holds conventions, anti-patterns, commands and pointers, at most 200 lines. The code map
moves to [the code map reference](../reference/code-map.md), still built from module docstrings by
`scripts/gen_code_map.py`, with the same stale check in prek and CI. `AGENTS.md` links to it.

### Consequences

- Good: the file every session reads is a fraction of its former size.
- Good: the map can't drift from the code; the check fails when it does.
- Bad: an agent that wants the map follows one link. `AGENTS.md` says where it is.

### Confirmation

`tests/test_agents_md.py` fails when `AGENTS.md` passes 200 lines, holds a generated table, or doesn't link to the code
map. `python3 scripts/gen_code_map.py --check` fails when the map is stale.
