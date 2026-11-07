---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# Descriptions name the runners ws supports today

## Context and Problem Statement

The tagline said that ws works in any AI, and the npm pointer package listed Codex among the supported tools. ws has two
runner adapters, Claude Code and OpenCode, and no Codex adapter. A reader who relied on either claim would be misled
(this follows from [0003](0003-public-from-the-first-commit.md)).

## Considered Options

- Keep the broad wording as an aim.
- Say what ws does today, and name the runners when a page lists them.

## Decision Outcome

Chosen option: the tagline is "Your memory, skills and rules, across your AI coding tools." Any page that lists
supported runners lists the adapters in `src/ws/adapters/` and no others. The npm pointer package says Claude Code and
OpenCode. When an adapter is added, its pages are updated in the same pull request.

### Consequences

- Good: no claim goes beyond the code.
- Bad: the list is a little longer to maintain than a broad phrase.

### Confirmation

Judged in review against the folder `src/ws/adapters/`. The tagline also appears in `pyproject.toml`,
`src/ws/brand.py` and `src/ws/cli.py`; those follow in a code change.
