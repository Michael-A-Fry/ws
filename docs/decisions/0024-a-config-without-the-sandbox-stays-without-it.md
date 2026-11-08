---
status: accepted
date: 2026-11-09
decision-makers: maintainer
---

# A config written without the sandbox stays without it until the policy says so

## Context and Problem Statement

The sandbox block became part of what ws writes into a runner's own settings in 0.1.8, on by default. A settings
file that ws wrote before then has no block (`WS_SANDBOXED` is `0` in it). When a newer ws rewrites such a file, the
default would add the block. A sandbox that cannot start (no bubblewrap or socat) makes Claude Code refuse to start, so
an upgrade could turn a working machine into one that cannot open a session, with the person having asked for nothing.

## Considered Options

- Follow the default: every rewrite adds the block.
- Keep what the record says: entries written without the block stay without it, whatever the default.
- Keep what the record says, unless the policy file itself names `sandbox`.

## Decision Outcome

Chosen option: keep what the record says, unless the policy file has a `sandbox` key under `[gate]`
(`ws.userconfig.keeps_sandbox_off`). The record is the install manifest's `WS_SANDBOXED` entry, or the settings file's
own value when the file holds ws's hooks and the record is lost. The policy file is read as written, because the loaded
policy cannot tell a default from a choice. This applies to `ws install` and `ws upgrade` alike, so nothing a person
does after an upgrade switches the block on behind them.

### Consequences

- Good, because an upgrade never changes what the sandbox does on a machine that did not have it.
- Good, because the way to turn it on is one line in the policy, and `ws doctor` WS012 says so.
- Bad, because a person who recorded `sandbox = false`, then deleted the line expecting the default, stays unsandboxed
  until they write `sandbox = true`. WS012 names that line.
- Bad, because the 0.1.8 default (on for every rewrite) now holds only for new installs and for policies that name it.

### Confirmation

`tests/test_upgrade.py` (`test_ws_install_does_not_switch_the_sandbox_on_either`,
`test_a_policy_that_says_sandbox_true_switches_it_on`, `test_a_sandbox_that_was_on_stays_on`) and
`tests/test_userconfig.py` (a foreign `WS_SANDBOXED` is still a conflict).
