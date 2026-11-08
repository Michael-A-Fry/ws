---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# One message store, with the bodies of walled classes kept apart

## Context and Problem Statement

Sessions on different runners and providers need to leave each other messages, and a lead needs to reach a session that
is busy. Two things make that harder than a mailbox. A session can't be trusted to say who it is, because a shell command
can set any environment variable or argument. And a message is a way for data to leave a data class: a walled class's
text must stay in its zone, while a viewer needs to show who wrote to whom and when.

## Considered Options

- One database per session, each holding its own inbox.
- One shared database holding everything, bodies included, with a rule that readers don't show walled bodies.
- One shared database holding the envelope of every message and the body of a non-walled one, with the body of a message
  to a walled session kept in a store inside that session's repo.

For who is calling:

- Take the sender from an argument, a flag or the environment.
- Take it from the process tree: the runner's process is stamped in the session record by the start hook, and a caller is
  the session whose stamp is an ancestor of the calling process.

## Decision Outcome

Chosen option: one shared SQLite database (`<state>/msg/messages.db`, WAL, schema `ws.message/1`) with the envelope of
every message, and the body of a message to a walled or counts-only session in `<repo>/.ws/messages/<class>.db`, inside
the paths that class covers, in a folder that ignores itself in git. The shared file has no body, no hash of a body (a
short or guessable body can be found from its hash) and no free-text field for those rows. A walled session sends fixed
kinds only, with no text, key or thread name. What may come in is the receiving class's `inbound` setting, and nothing
makes an exception for a reply or a task the sender owns. The viewer and `ws msg watch` read the envelope table only.

The sender is the session whose runner process is an ancestor of the caller (`ws.session.resolve_caller`), nearest first.
Nothing the caller is given takes part. A process with no such ancestor, such as one that double-forked, is not a session
and can't send. The receiver's class is looked up from the policy by the receiver's folder at send and at read, never
taken from the message.

An idempotency key is stored as the hash of the sender and the key, so a key that was chosen by one session is not
text in the shared file. A thread name can't be set on a message to a walled class.

### Consequences

- Good: a read of the whole shared file finds no walled body; deleting a walled repo deletes its bodies, and `prune` drops
  the envelope rows that pointed at it; a forged sender, class or key has nothing to act on.
- Good: the shared store is one file for the viewer, export and retention to read, and the schema grows by new tables
  and columns.
- Bad: a message to a walled session writes two files in one transaction. SQLite keeps each file whole, but in WAL mode
  a crash in the middle of the commit could leave the body without its envelope or the reverse. The body goes in
  first, the hourly prune removes bodies past their retention, and a read of a message whose body is missing says
  `gone`.
- Bad: with `[classes]` in the policy, a full class defaults to taking messages from the lead only, so a worker can't
  reply to the lead until that class says `inbound = "allowlist"` and names the sessions. That is the policy's choice to
  make, not a rule in the store.
- Bad: the repo root of a walled receiver is kept in the shared file so `prune` can tell the repo is gone. The session
  registry already holds the same folder.
- Bad: inside the wall the state folder is read-only, so a send from there fails with a typed `unavailable` error.
  Delivery across the wall is not built yet.

### Confirmation

`tests/test_msg.py` (the walled-class rules, the canary read of the whole shared file, export, forged senders and class
headers, replay, the caps, the typed store failures, retention, deletion), `tests/test_session_registry.py` (the runner
stamp and `resolve_caller`, including a real orphaned process) and `tests/test_mcp_server.py` (the tools and the bounded
frames).
