---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# Reserve before a run, settle after it

## Context and Problem Statement

`ws run` checked the month's spend against the cap before it started, and counted the run afterwards. Two runs that
started together each saw the same room and both went ahead. A run could spend what its own timeout and turns allowed,
whatever was left. Nothing held a concurrency limit or the host's memory, and a job that ran from a timer on a login
(rather than a key) had nothing to say how much of that login was left. A purpose that must never spend more than its share
(evals against jobs, on one key) had no counter of its own.

## Considered Options

- Keep the check, and add a lock around "read spend, start run".
- Count spend in a service that runs all the time.
- Reserve in one SQLite database, in one transaction, before any run starts, and settle from what the run used.

## Decision Outcome

Chosen option: reserve in SQLite. One WAL database in the state folder (`queue/queue.db`) holds a receipt for every run.
Admission is one `BEGIN IMMEDIATE` transaction that reads the month's records and the open reservations, checks every
counter that applies (the profile's monthly cap, the job's purpose cap in `[budget.purposes]`, turns and seconds in
flight, a slot in the lane, host memory) and writes the reservation, or says which counter said no. `ws run`, the queue's
drain and `ws queue slot` use the same code, so there is one path. A reservation is settled once, from the run's actual
cost, in the code that writes its record, and a crash is reaped by the owner's process id and start time.

Two decisions inside it. Money that is short resizes the reservation to what is left and that sum is passed to Claude
Code as `--max-budget-usd` (unless the job says `strict`); OpenCode has no such flag, so there the reservation holds the
money and only the settle corrects it. And a store that cannot be used (locked, full, unreadable) refuses the run: the
alternative, running without a reservation, is the very gap this closes.

The queue engine is one file of standard-library code (`ws._vendor.sqlqueue`) with claim ids that fence a stale holder,
idempotency keys and lease expiry. Message delivery is meant to reuse it, so its API is documented in its own docstring.
Subscription limits are read from what Claude Code gives its statusline command (`ws usage statusline`), never by calling
the provider's usage endpoint with the person's login, and unattended work is refused unless it has an API key or a local
provider.

### Consequences

- Good: two admissions racing for the last dollar or the last slot admit one; a spend cap holds for a run that starts
  late; a purpose cannot take another's room; a run is never started without a reservation.
- Good: the run record (schema 3) says what was reserved and why a run stopped.
- Bad: one more file to keep private and one lock to wait for. A locked store refuses a run that would otherwise have
  gone, which is deliberate. For the few milliseconds between a run writing its record and settling its reservation the
  run is counted twice, which only ever errs on the side of keeping the cap.
- Bad: a run that is not stopped by the runner's own limit (OpenCode, and any run whose estimate was only a default)
  can still spend past its reservation; the settle then counts what it really spent, and the next admission sees it.

### Confirmation

`tests/test_queue.py` (every counter reserved or none, the resize and the budget flag, a purpose refused at its own cap,
two admissions racing for the last slot and the last dollar, settle once, spend counted once, the month boundary, a locked
store refusing the run, a dead owner reaped, the retry classes) and `tests/test_node.py` (the footprint ledger).
