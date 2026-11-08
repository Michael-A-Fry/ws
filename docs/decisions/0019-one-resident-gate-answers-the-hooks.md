---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# One resident gate answers the hooks

## Context and Problem Statement

Every tool call a runner makes starts a hook, and each hook started Python and loaded ws: about 285 milliseconds a call.
Each guard that needs its own hook would add another process, run in parallel with the rest, so the order of the checks
wasn't fixed. The decision channel also has to be one that another user's process on the machine can't answer.

## Considered Options

- Keep one process for each hook call, and make the import lighter.
- A resident process on a loopback port, called by an `http` hook.
- A resident process on a Unix socket in a private folder, called by a thin command hook that decides for itself when the
  process can't answer.

## Decision Outcome

Chosen option: the Unix socket and the thin command hook. `ws gate serve` runs every guard in one fixed order (the
policy's rules, then the guards by declared priority, the first deny ending it) and logs each guard's verdict. The
hook is
`python -I -S gateclient.py`, which imports the standard library only. Any failure to get an answer (no daemon, a dead
socket, a time-out, a reply that doesn't read) runs the same code in the client's own process, so the answer is the same
and only slower; a missing daemon never allows a call. Every gate hook sets `onFailure: "block"`, so a crash or a time-out
blocks, and `ws doctor` requires a Claude Code that honours it (2.1.295).

A loopback port can be bound first by any process on the machine, and a hook that finds the wrong process there would
take its allow. A socket in a folder only the user can enter, checked on every call (owner, mode, no link, the peer's user
id, on Linux the peer's interpreter and `-m ws gate serve` command line, a reply that echoes the request's random id),
can't be answered by another user. It can be answered by the user's own process when nothing keeps that process out of
the run folder: with the sandbox on the folder is read-only to the session, and without it the gate's static check of
writes is the barrier (see known limits). Every failure to get an answer falls back to the check in the client's own
process, so the worst a same-user process gets by stopping the daemon is the slower path.
The client's clock is part of the decision: it waits at most 1 second to connect and 11 for the reply, then runs the check
itself (at most 12 seconds), all inside the 30 second time-out written into the hook, because a runner that kills a hook
turns `onFailure: "block"` into a deny.

### Consequences

- Good: a warm call takes about 50 ms where it took about 285 ms; both runners use the same client and get the same
  decision; the order of the checks is written down and tested.
- Bad: a resident process to start, stop and keep in step with the installed code (it exits when ws changes under it).
  A process of the same user that can write the state folder can still stand in for the daemon, as it can edit the policy;
  the sandbox keeps that folder read-only.

### Confirmation

`tests/test_gate_daemon.py` (the real decision path for both runners, the daemon killed mid-session, a socket that
isn't the daemon's, an upgrade under the daemon) and `tests/test_gate_dispatch.py` (the order, the first deny, the
shared decision path).
