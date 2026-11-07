---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# The gate and the policy fail closed

## Context and Problem Statement

The gate answers a runner's hook before a tool call runs. If it can't read the call, load the policy or parse a rule,
it has to choose between letting the call through and stopping it. A policy that loads in part would also give a person
protection they don't know they lack.

## Considered Options

- Allow when ws can't decide, so a fault never blocks work.
- Deny when ws can't decide, and load the policy and the secrets registry whole or not at all.

## Decision Outcome

Chosen option: deny. A payload the gate can't read, a policy it can't load or a rule it can't parse is a deny. The
policy and the secrets registry either validate completely or the command refuses. A hook that only advises exits 0 and
says why on stderr. The matching rule for code: never classify an error by its message; raise a typed error with a kind
and a hint, and branch on that.

### Consequences

- Good: a fault makes ws stricter, never looser.
- Bad: a broken policy file stops work until it's fixed. The error says which key and how.

### Confirmation

`tests/test_gate.py::test_check_fails_closed_on_a_payload_it_cannot_read`,
`tests/test_gate.py::test_check_fails_closed_on_a_broken_policy_and_on_a_crash` and
`tests/test_config_status.py::test_policy_and_secrets_fail_closed_not_partially`.
