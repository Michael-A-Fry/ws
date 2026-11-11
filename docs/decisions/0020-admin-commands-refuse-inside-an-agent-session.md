---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# Admin commands refuse to run inside an agent session

## Context and Problem Statement

Some `ws` commands change what gates a session or hold what it must not reach: the secrets store and the broker,
`approve`, `uninstall`, `upgrade`, `gate init` and `gate generate`, `install --gate`, `skills adopt` and `restore`,
`config fallback`, `session prepare` and `session start`, `node join` and `remove`, and the `queue` commands that add,
cancel or run work. A check that reads only the command line a session writes has to know every way of writing a
command, and the line it reads isn't always the line that runs. The program that runs the command knows for certain
which command it is.

## Considered Options

- Keep widening the text matcher for each spelling found.
- Make `ws` check, once a command is parsed, whether it runs inside an agent session, and refuse the admin commands there.
  The matcher stays as the first line.
- Move the admin commands to a separate program that a session can't reach.

## Decision Outcome

Chosen option: `ws` refuses. `ws.cli.main` calls `ws.agentsession.guard` after parsing, for every command, so a new admin
command can't forget it; the admin commands are one table that the matcher reads too. A call is in a session when
`WS_GATED_SESSION` is set, or when a process above it's an agent runner (claude, opencode, codex, gemini, goose), read
from `/proc` or `ps` and matched on a process's name or the program it runs, never on command text. The user-level
install leaves the marker out on purpose, so the process tree is what recognises a session started the usual way. Under
a runner, a person is let through only on the terminal the runner itself holds with the command in its foreground,
because a session can make a pty of its own; with no runner above it a terminal is enough. The marker refuses with a
terminal too. A tree that can't be read counts as a session. Native Windows has only the
marker. The refusal is a typed error of kind `refused`, exit 3, with the hint to run it in your own terminal.

There is no environment switch to turn the check off, because that would be the way round it. Tests hand the tree and the
terminals to `guard` as arguments.

`session start` stays in the matcher's table only: the runner's own SessionStart hook runs it inside the session with no
terminal, and refusing it would stop the session registry being written. `broker serve` is the other: `ws run` starts the
broker for a job's secrets with no terminal, and a lead dispatches jobs from inside a session, so refusing it would break
`ws run` with secrets. A typed `ws broker serve` is still denied by the matcher; every other broker command is refused by
`ws`.

### Consequences

- Good: a spelling the matcher didn't think of is still refused by `ws`; one table serves both; the check costs nothing
  for a command that isn't admin, and reads the process tree only when the marker and the terminals haven't settled it.
- Bad: a command that has left the tree (`tmux new -d`, a double fork, a service manager) with the marker unset looks
  like a person's own pane or a service, so `ws` can't refuse it; the matcher reads those spellings first, and the wall
  and the sandbox stand behind it, as they do for `ws approve`. A person who runs an admin command with its output
  piped, under a runner, is refused (run it with a terminal on both ends).
- Bad: the test suite, run by an agent, is under a runner, so `tests/conftest.py` reads the real tree as empty.

### Confirmation

`tests/test_admin_in_session.py` (each spelling refused by the matcher and by `ws`; each admin command; the marker, the
runner, the terminal and the unreadable tree; non-admin commands never refused; the tree read from a planted `/proc`) and
`tests/test_policy_classes.py` (the launcher and path spellings in the matcher).
