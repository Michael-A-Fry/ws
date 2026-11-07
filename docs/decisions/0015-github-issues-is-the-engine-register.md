---
status: accepted
date: 2026-11-07
decision-makers: maintainer
---

# The engine's register is GitHub Issues

## Context and Problem Statement

A project needs one place to see what's planned, broken or wanted. Two lists (a file in the repo and the issue tracker)
drift apart, and a contributor can only act on the one they can see and comment on.

## Considered Options

- A `TODO.md` file as the register.
- GitHub Issues, with labels for kind, area, priority and size.

## Decision Outcome

Chosen option: GitHub Issues is the register for ws. Anyone files a bug, gap or idea there with `kind:`, `area:`, `P0` to
`P3` and `size:` labels, or `triage:needed`. A vulnerability goes to a private security advisory, never an issue.
`TODO.md` is a short list of what's being worked on now, not a second backlog.

### Consequences

- Good: contributors and the maintainer use the same list, with history and links to pull requests.
- Bad: the register is outside the repository, so a clone alone doesn't carry it.

### Confirmation

Judged in review. `CONTRIBUTING.md` and `AGENTS.md` both state where to file.
