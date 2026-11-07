---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# Record each engine decision once, in MADR 4.0.0

## Context and Problem Statement

Reasons for a design choice were scattered across pull request bodies, write-ups and a long narrative log. A reader
could find what ws does but not why, or whether a choice still stands.

## Considered Options

- Keep the narrative log.
- One short record per decision in a standard format, never edited once accepted.

## Decision Outcome

Chosen option: one file per decision in `docs/decisions/`, named `NNNN-short-title.md`, in the
[MADR 4.0.0](https://adr.github.io/madr/) shape: status, date, context, options, outcome, consequences and a
confirmation line. A record isn't edited after it's accepted, except to correct a fact it states wrongly (say so in the
text, as [0010](0010-package-name-ws-aitoolkit.md) does) or to change its status. A changed decision gets a new record
and the old one becomes `superseded by`. The index is [README.md](README.md).

### Consequences

- Good: the why sits next to the code, one decision per file, with a date.
- Good: each record names the test or check that keeps it true, or says it's a judgement.
- Bad: a small decision costs a file. Only a choice a later reader would otherwise ask about needs one.

### Confirmation

`scripts/check_docs_integrity.py` checks every link in a record and in the index. `tests/test_docs_integrity.py` runs
that check on the repo itself.
