---
status: accepted
date: 2026-11-14
decision-makers: maintainer
---

# A scheduled job keeps a run contract: six lines, a fenced lock and a capped log

## Context and Problem Statement

A timer runs unattended. One that hangs, fails quietly, runs twice at once or fills the disk is found by accident. What
must a job file say before it may have a timer, and what must every run guarantee?

## Considered Options

- Write a scheduler of our own that knows jobs, retries and next-run times.
- Adopt the init system's own features (`OnFailure=`, `TimeoutStartSec=`, `Persistent=`, a slice) and check the job file
  in `jobs.problems`.

## Decision Outcome

Chosen option: adopt the init system. A job file with a `schedule` must carry six lines: `timeout_s` (maximum runtime),
`on_failure` (the failure hook), `log`, `uninstall`, `expected_rate` and `data_class`; a job with a runner also needs
`max_turns` and `budget_usd`. `ws config` and `ws doctor` report a file without them; `ws run` and the timer still run
what loads, so an upgrade never stops a job in the night. Each generated service carries `OnFailure=`,
`TimeoutStartSec=` and a shared slice with a CPU and memory share; each timer is `Persistent=true`.

A run holds a lock file with a heartbeat (its mtime) and a fence number, not a pid. A second start finds it fresh and
skips, so two ticks make one run; a holder that stopped beating is taken over once, with one `died` record. The next
run time is the init system's, advanced before it starts the unit, so ws keeps none. A job log over 20 MB is rotated
and keeps three files, and the old ones drop lines below INFO.

### Consequences

- Good: no scheduler to maintain; a stub that hangs, fails or doubles is a test.
- Bad: a scheduled job file written before this fails the check until it gets the six lines (it still runs).
- launchd has no `OnFailure=`; there the alert is sent by `ws run`'s own exit path.

### Confirmation

`tests/test_jobs.py` fails a scheduled job file for each missing line and for a runner job without its caps.
`tests/roles/test_role_schedulers.py` checks each rendered service for `OnFailure=`, `TimeoutStartSec=` and the slice.
`tests/test_joblock.py` checks a double tick runs once, a stale holder is taken over with one died record, and the log
cap.
