---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# A headless job runs inside the same wall as a launch, built by the same code

## Context and Problem Statement

`ws gate launch` builds the wall for an interactive session: a launch copy of the settings for the project, the class
launch file merged in, the project's load paths locked, the account checked against the class, and for OpenCode a
bubblewrap (or `sandbox-exec`) plan with private data folders and ws's proxy. A headless `ws run` job needs the same
wall, built per job for its folder. Two code paths that build the same wall drift apart, so the question is where the
wall is built.

## Considered Options

- Build the wall for jobs separately in `ws.run`.
- Take the wall out of the two launch paths into shared helpers in `ws.sandbox` (`claude_launch_text` for the Claude
  Code launch copy, and one context manager for the OpenCode plan, its private folders, the project lock and the proxy),
  and call them from the launch paths and from `ws.run`.
- Leave jobs outside the wall and document it.

## Decision Outcome

Chosen option: shared helpers, because a job then gets the wall a launch gets by construction, and a change to the wall
reaches both. A job the wall can't be built for is refused, and the refusal is a run record (`refused:wall`), so the
person sees why. The checks that need nothing but the policy and the folder (the account, the class launch file, the
project's load-path files) run before anything is reserved, so `--dry-run` lists them.

### Consequences

- Good: one wall, one place to change it; a job and a launch in the same folder are bound alike.
- Good: a job that can't be walled is refused with its reason on record, never run bare. `[gate] sandbox = false` is
  the one switch that leaves a job bare, as it leaves a launch.
- Bad: an OpenCode job needs bubblewrap or `sandbox-exec` on the machine.
- Bad: a job in a class's folder started from an account that class doesn't name is refused, and so may a scheduled
  job. The refusal names the class.
- Clean-up is guarded step by step: a step that raises (letting go of the project, stopping the proxy, releasing the
  wall) prints one warning line and the steps after it still run, so the leases, the run record, the reservation settle
  and the wall's private tmp never depend on an earlier step. A refusal found while the wall is built (a missing launch
  file, a project the runner's check refuses) is recorded `refused:wall`, like any other refusal there.
- Not walled: the runner's `--version` probe, which `ws run` makes before the wall to record `runner_version`. It runs
  the runner's binary with no project, no prompt and no secret.

### Confirmation

`tests/test_run_wall.py` runs a Claude job and checks its per-run launch copy names the project's load paths, runs an
OpenCode job and checks it starts under bubblewrap with the project locked, and checks a job the wall can't be built
for is a `refused:wall` record. `tests/test_sandbox.py` pins the launch paths to the same helpers.
