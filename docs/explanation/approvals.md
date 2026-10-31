# Approvals

An approval is the rule that a command you marked `ask_commands` runs only after you, at a terminal of your own, say yes
to that exact call, once.

## The rule

The gate refuses the first try and prints an id. You approve the call with `ws approve ID`. The session then retries the
identical call, and it runs. The approval is used up by that run.

## Why it exists

Some commands you want an agent to be able to run, but not on its own say-so: a push to a remote, a deploy, a delete in
a shared folder. A blanket allow is too loose and a blanket deny stops useful work. An approval puts a human in the loop
for exactly the calls you choose.

## How ws enforces it

The code is `src/ws/approvals.py`, with `src/ws/gate.py` calling it.

- **Bound.** The id is a SHA-256 over the runner, the kind of call, the exact command, the folder and the session id.
  A different command, folder, session or runner is a different id and needs its own approval. One changed byte is a new
  call.
- **Single use.** The granted record is claimed with an atomic rename, so two racing calls can't both use it.
- **Short-lived.** A pending record expires 10 minutes after the request, and a granted one 10 minutes after the grant.
- **Human only.** `ws approve` refuses when standard input isn't a terminal, and when the `WS_GATED_SESSION` marker is in
  its environment. The matcher also denies `ws approve` as a shell command.
- **You see what you grant.** `ws approve` shows the exact call, JSON-escaped, and asks `approve? [y/N]`. It grants the
  record it showed, not whatever the folder holds a moment later.
- **Tamper checks.** A granted record whose fields no longer hash to its id, whose timestamps are out of order or in the
  future, or that's unreadable, is refused and removed. The call goes back to asking.

Records live in `$WS_HOME/approvals/pending/` (written by the gate) and `$WS_HOME/approvals/granted/` (written only by
`ws approve`). Both are ws control paths, so a session can't write them with its own tools. Inside the OpenCode wall
the hook can't consume a grant, because `granted/` is read-only there. The launcher keeps a socket pair outside the
wall and hands the runner one end. The shim claims the grant through it, and the launcher consumes the record.

A command that matches both `ask_commands` and `deny_commands` stays denied.

## What approvals don't stop

- **A determined session that can run any program outside the wall.** A session that allocates its own terminal and
  clears the marker defeats the first two checks. The matcher rule and the wall are what stand then. Approvals stop a
  model from approving itself by accident or by a plain instruction. They're not a defence against an attacker with
  free run of your machine.
- **A different call.** An approval covers one exact call. A model can ask for another and get another id.
- **A call that doesn't reach the gate.** A command the matcher can't see inside (an interpreter running a
  script) isn't an `ask_commands` match.

## Do it

Follow [Approve a command](../how-to/approve-a-command.md).
