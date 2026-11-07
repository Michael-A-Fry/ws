---
status: accepted
date: 2026-10-31
decision-makers: maintainer
---

# Licence the code Apache-2.0

## Context and Problem Statement

The repository is public from the first commit, so the licence is chosen before anyone else reads the code. It has to
let a company, a team or one person use ws without asking, and say what happens to patents.

## Considered Options

- Apache-2.0.
- MIT.
- A copyleft licence.

## Decision Outcome

Chosen option: Apache-2.0 for the code, with a `NOTICE` file. It's permissive and carries an explicit patent grant.
The terms for outside contributions are in `CLA.md`; a later record changes them if they change.

### Consequences

- Good: anyone can use and adapt ws, including inside a company.
- Good: third-party code must be compatible, so a copyleft dependency can't enter the core.
- Bad: others may also sell it. That's accepted for the sake of adoption.

### Confirmation

`pyproject.toml` declares `license = "Apache-2.0"` and ships `LICENSE` and `NOTICE`. `scripts/check_docs_integrity.py`
fails when either file is missing.
