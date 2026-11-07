---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# The core uses the Python standard library only

## Context and Problem Statement

ws sits between a person and the tools that run commands for them, so what it imports is part of what a person has to
trust. Each dependency adds a supply-chain path, an install step and a thing that can break on another machine.

## Considered Options

- Use well-known libraries for the CLI, TOML writing and HTTP.
- Standard library only in `src/ws`, with any exception argued in a pull request.

## Decision Outcome

Chosen option: standard library only in `src/ws`, on Python 3.11 or newer. A new dependency needs a written reason in the
pull request and a pin in `uv.lock`. Package data (templates, HTML, skill text, schemas) ships as Python strings,
because `ws secret wall` copies only `.py` files.

### Consequences

- Good: `pipx install` pulls in nothing else, and the audit surface is ws's own code.
- Bad: ws writes a few small helpers (a front-matter reader, a locked log) that a library would give.

### Confirmation

`tests/test_principles.py::test_src_ws_imports_only_the_standard_library` and
`tests/test_cli.py::test_package_has_no_runtime_dependencies`.
