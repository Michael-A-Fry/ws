---
status: accepted
date: 2026-10-31
decision-makers: maintainer
---

# An agent drafts; a person sends

## Context and Problem Statement

Sending is an act on the outside world and can't be taken back. An assistant that drafts a reply is useful. One that
sends it can do harm in public.

## Considered Options

- Draft-only: the agent prepares, a person sends.
- Tiered: a person widens what an agent may send, step by step.
- Free: the agent sends when it judges fit.

## Decision Outcome

Chosen option: draft-only is the default. The `dial` in `policy.toml` is `prepare`, `allowlist` or `act`, and ws only
ever writes `prepare`. A person may widen it by hand; the sending then happens in a process the agent doesn't control,
never with a credential the agent holds. Enforcement beyond the dial isn't built yet, and
[Draft-only sending](../explanation/draft-only-sending.md) says what exists and what doesn't.

### Consequences

- Good: a wrong draft costs nothing.
- Good: the Rule of Two reads the dial, so a `prepare` policy never counts a send as a way out.
- Bad: the rule isn't enforced for a send ws can't see. [Known limits](../explanation/known-limits.md) lists it.

### Confirmation

`tests/test_output_contract.py::test_setup_yes_and_install_never_write_dial_act` pins that ws writes `prepare` and never
`act`.
