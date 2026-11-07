---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# Name the package `ws-aitoolkit` and the command `ws`

## Context and Problem Statement

The short name `ws` is what people type, but the PyPI project `ws` is taken, and the npm package `ws` is a different
tool with no command. The first record of this decision (31 October) named the package `ws-toolkit`, which was never
the published name. This record corrects it and keeps the number.

## Considered Options

- Take another word for the whole project.
- Keep `ws` for the command and the repo, and publish the package under a longer name.

## Decision Outcome

Chosen option: the command and the repo are `ws`; the package on PyPI, and the pointer package on npm, are
`ws-aitoolkit`. Install with `pipx install ws-aitoolkit` or `uv tool install ws-aitoolkit`.

### Consequences

- Good: the command stays short.
- Bad: the package name and the command differ, so install pages state both.
- Bad: a page that still says `ws-toolkit` is wrong. The docs check rejects an install line that uses it.

### Confirmation

`scripts/check_docs_integrity.py` fails an install command that names `ws-toolkit`. `tests/test_pypi_readme.py` checks
the PyPI description and project links of `ws-aitoolkit`.
