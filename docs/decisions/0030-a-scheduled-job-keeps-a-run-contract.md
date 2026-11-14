---
status: accepted
date: 2026-11-14
decision-makers: maintainer
---

# A scheduled job keeps a run contract of six lines, a fenced lock and a capped log

## Context and Problem Statement

A timer runs unattended. One that hangs, fails quietly, runs twice at once or fills the disk is found by accident. What
must a job file say before it may have a timer, and what must every run guarantee?

## Considered Options

- Write a scheduler of our own that knows jobs, retries and next-run times.
- Adopt the init system's own features (`TimeoutStartSec=`, `Persistent=`, a slice, and `OnFailure=` once an alert
  unit exists) and check the job file
  in `jobs.problems`.

## Decision Outcome

Chosen option: adopt the init system. A job file with a `schedule` must carry six lines: `timeout_s` (maximum runtime),
`on_failure` (the failure hook), `log`, `uninstall`, `expected_rate` and `data_class`; a job with a runner also needs
`max_turns` and `budget_usd`. `ws config` and `ws doctor` warn about a file without them (a warning, exit 0, not a
failure); `ws run` and the timer still run
what loads, so an upgrade never stops a job in the night; a bad `timeout_s` or `expected_rate` is the check's business
and falls back to "not given" for the timer. Each generated service carries `TimeoutStartSec=` (the job's limit and a
margin, or `infinity` for a file with no `timeout_s`, which is what a oneshot had before), and a shared slice with a
CPU and
memory share; each timer is `Persistent=true`. `on_failure = "alert"` declares the failure hook; each service's
`OnFailure=` names the alert unit, which is generated with it. Nothing reads
`expected_rate` yet; the dead-man check does.

A run holds an advisory `flock` on a lock file, with a fence number in the file, not a pid and not a heartbeat. The
kernel drops the lock when the holder dies, however it dies, so a suspended or slow run keeps it and a dead one never
does.
A second start finds it held and skips, so two ticks make one run. A new holder that finds the file still marked running
has taken over from a dead one, and writes one `died` record. The next run time is the init system's, advanced before it
starts the unit, so ws keeps none. A job log over 20 MB is rotated and keeps three files, and the archives drop lines
below INFO.

### Consequences

- Good: no scheduler to maintain; a stub that hangs, fails or doubles is a test.
- Bad: a scheduled job file written before this is a warning until it gets the six lines (it still runs).
- Each service rotates its log after it stops (`ExecStopPost=`, `ws watch rotate`), so jobs that run a ws command
rotate too.

### Confirmation

`tests/test_jobs.py` fails a scheduled job file for each missing line and for a runner job without its caps.
`tests/roles/test_role_schedulers.py` checks each rendered service for `TimeoutStartSec=` (and `infinity` without a
`timeout_s`) and the slice.
`tests/test_joblock.py` checks a double tick runs once, a dead holder is taken over with one died record, and the log
cap. `tests/test_joblock_race.py` races real processes: one winner, one died record, and a live holder is never taken
over.

### The alert and the dead-man check

`OnFailure=ws-alert@%n.service` runs `ws watch alert`, which writes one `not-automatic` line to `logs/alerts.jsonl` and
one ntfy line through the notify role; both carry the job name, unit and exit status, never a log line. An hourly
`ws-watch-deadman.timer`, a unit outside the jobs it watches, raises one alert for each job with no run record in twice
its `expected_rate`, and the next only after the job has run and gone overdue again. It runs on the machine it watches,
so it can't see that machine down. It measures jobs that run through `ws run` (they leave run records under their
name), not jobs that run a ws command, and exits 3 when it found one overdue (the unit counts that a success; any
other exit fails the unit and alerts). A failed send is retried at the next check. launchd has no `OnFailure=`, so
`ws run` sends the alert from its own exit path.
`tests/test_jobwatch.py` pins each of these.
