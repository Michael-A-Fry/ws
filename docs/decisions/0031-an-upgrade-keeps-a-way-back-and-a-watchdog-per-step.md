---
status: accepted
date: 2026-11-14
decision-makers: maintainer
---

# An upgrade keeps a way back and a watchdog on each step

## Context and Problem Statement

A release swap that stops half-way, or lands a build that starts but doesn't work, leaves the person with a broken
install and no recorded way back. What does the upgrade command keep so that going back is one command, and a stuck
step can't leave the machine half-changed?

## Considered Options

- Leave rollback to the installer: the person runs it again with an older tag.
- Record each swap, offer `--rollback`, give each step a time limit, and accept a health command of the person's own.

## Decision Outcome

Chosen option: record and guard. Each swap is a promote, written to `<state>/upgrade-history.jsonl` with the version it
came from. `ws upgrade --rollback` reinstalls that version (or `--version`) and rewrites the hooks for it. The installer
gets 600 seconds and the hooks step and the health check 120 each (`--install-timeout`, `--step-timeout`); a step that
runs past its limit is stopped. `--check COMMAND` runs after the swap: a health command that must exit 0 (a service that
must answer, a page that must load). An installer that times out, a hooks step that times out, or a check that fails or
times out rolls the swap back at once and is recorded as `auto-rollback`. An installer that simply fails changed nothing
and isn't rolled back. The release itself is whatever the installer installs, so a release tree or a package manager
plugs in through `--installer`.

### Consequences

- Good: one command each way, a history the person can read, and no step that can hang an upgrade for ever.
- Good: a person's own service check gates the swap without ws knowing what the service is.
- Bad: the way back is another install, so it needs the network or a local installer, and the old version's tag must
  still exist.
- The upgrade is still refused inside a session, so none of this runs unattended.

### Confirmation

`tests/test_upgrade.py` covers the history, `--rollback`, a failing and a passing check, a hooks step and an installer
that run past their limits, a rollback that fails, and the watchdog on a real hanging process.
