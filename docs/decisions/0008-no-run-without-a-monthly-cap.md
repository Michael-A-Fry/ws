---
status: accepted
date: 2026-10-31
decision-makers: maintainer
---

# Start no paid run without a monthly cap the person set

## Context and Problem Statement

An unattended job that loops can spend real money before anyone looks. ws can't know a provider's prices, so it
can't set a sensible cap for a person.

## Considered Options

- A default cap that ws chooses.
- No cap, with spend shown after the fact.
- A cap the person sets, checked before every run, with a refusal when it's missing or reached.

## Decision Outcome

Chosen option: a cap the person sets. `ws run` refuses a job when `[roles.budget]` has no `monthly_cap_usd`, when this
month's spend has reached it, or when the runner can't price the model and the job hasn't set `allow_unpriced = true`.
A refusal exits 3 and still writes a run record with status `refused`. ws ships no price list.

### Consequences

- Good: a stuck loop is bounded by a number the person chose.
- Good: a refusal is visible in the run record, not silent.
- Bad: a new install can't run a job until a cap exists.

### Confirmation

`tests/test_run.py::test_no_cap_refuses_and_still_records` and `tests/test_run.py::test_spend_at_the_cap_refuses`.
`ws doctor` reports the same setting as WS103.
