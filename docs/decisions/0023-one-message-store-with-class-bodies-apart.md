---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# One message store, with the bodies of walled classes kept apart

## Context and Problem Statement

Sessions on different runners and providers need to leave each other messages, and a lead needs to reach a busy session.
Two things make that harder than a mailbox. A session can't be trusted to name itself, because a shell command
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
Nothing the caller is given takes part. A process with no such ancestor, such as one that double-forked, isn't a session
and can't send. The receiver's class is looked up from the policy by the receiver's folder at send and at read, never
taken from the message. The registry, the policy, the secrets registry and the database are found from the account's own
home folder (`ws.session.trusted_env`, from `pwd`), never from the environment: a command can set `WS_HOME`, `WS_*_DIR`,
`XDG_*` and `HOME`, and a registry, a policy and a store it made there would vouch for themselves. (Checking a record
against the environment's config folder was tried and fails the same way: the record is found through that environment.)
The cost: a person whose ws folders come from one of those variables is refused until ws reads the layout from the
runner's own start-up environment (`/proc/<pid>/environ`, `ps eww` on macOS), the stronger option left for later.

An idempotency key is stored as the hash of the sender, the recipient and the key, so a key that was chosen by one
session isn't text in the shared file, and the same key to two sessions is two messages. A thread name can't be set
on a message to a walled class.

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
  send it free text until that class says `inbound = "allowlist"` and names the sessions. The one exception is a fixed
  kind (`ack`, `done`, `counts` and the like, which carry no text) sent to a lead: the default lets it in from any
  session, a walled one included, so the done and ack flow works with no policy edit. An explicit `off` or `allowlist` is
  obeyed, and no fixed kind goes into a walled class.
- Bad: the repo root of a walled receiver, and the device it was on, are kept in the shared file so `prune` can tell the
  repo is gone and not merely unmounted. The session registry already holds the same folder.
- Bad: inside the wall the state folder is read-only, so a send from there fails with a typed `unavailable` error.
  Delivery across the wall isn't built yet.

### Amendment: A person sends (hub chat 1b)

Context: the person who owns the account sends from the hub or a plain terminal, and the record must say who wrote
(traceability, audit). Until now a send with no calling session was refused, which is also what an agent that has
escaped its ancestry gets. "No runner ancestor" is therefore not proof of a person: `setsid -f`, `nohup`,
`systemd-run --user`, `at`, `tmux new-window` and cron all give an agent process whose ancestors hold no runner.

Decision: a person send needs positive proof, and a switch that's off by default.

- `[msg] person_send` in policy.toml (default `false`) is the rollback: off, the behaviour is exactly the earlier one.
  A new key is needed because no existing setting says "this account lets its person send"; `[classes]` is about
  sessions and `[gate]` about what a session may do (DEC-059: a key that replaces no setting says why).
- On, a send with no calling session is a person send only when the process can read the per-boot token
  (`<data>/secrets/.person-token`, 0600, made by `ws msg person-token` from the person's own terminal at hub start)
  and it matches the digest, with this boot's id, in `<state>/identity/person.sha256`. The token sits in the secrets
  folder, which every policy already adds to `deny_read` (`policy.secret_read_rules`), so every launch wall hides it
  from a session. The digest sits in the identity folder, which is read-only inside the wall, so a token an agent plants
  (the hidden folder is an empty overlay it can write to) never matches. A loose, linked or foreign-owned file is no
  proof. `ws msg person-token` is an admin command, refused inside a session.
- The sender is recorded as `person:<account user>` (the user from the system's user database). `admit()` gets a
  synthetic, never-registered record built in one function (`msg.person_record`), with role `lead` and no class, and its
  own rate cap (`PERSON_RATE_PER_MINUTE`, 20) apart from the per-session one. Walls are unchanged: a walled class takes
  it only if its `inbound_allow` names `person:<user>`; the default is refused. The caps, the secret scan and the walled
  kinds apply as before. Only the command line takes this path; `ws mcp serve` never does.
- The envelope rows, `ws msg watch` and the list show `person:<user>`.
- The launcher step `ws session prepare` (what `ws dispatch` calls) writes `requested_by` into the launch token: the
  calling session's id, or `person:<user>` only when the same proof passes, else `unknown`. The start hook copies it
  to the identity file (the authority) and the session record; a resumed start keeps it; an older record reads as
  `unknown`. `ws session show ID` prints it. The record is the audit trail; there is no new log.

What this stops, and what it doesn't. The token is only as private as the wall that hides it:

- It stops: an agent in a launch wall (the secrets folder hidden, the identity folder read-only) from sending as the
  person, by any ancestry escape, and from minting its own proof.
- It doesn't stop: a session with the wall off (`--no-sandbox`) running as the same OS user, which can read the token.
  So with `[gate] sandbox = false` the person send is refused; ws can't see a `--no-sandbox` launch under a policy that
  says on. On macOS, and where the process id of a hook can't be had (`bind_peer(None)`), a person send is refused.
- The full fix is a separate OS user per class (C111), so a session can't read the account's files at all. Until then
  this is a wall-dependent control.

| Case | Result | Test |
|---|---|---|
| a walled shell, and a process it orphans with `setsid -f` (so also what nohup, `at` and cron give: still inside the wall's mount namespace) | the token is hidden and `person_proof` is False | `tests/e2e/test_sandbox.py::test_a_walled_shell_and_a_process_it_orphans_cannot_read_the_person_token` |
| no runner ancestor and no token readable | refused | `test_no_runner_ancestor_without_the_token_is_refused` |
| a token the agent writes itself | refused: no matching digest | `test_planted_token_without_the_digest_is_refused` |
| a token from an earlier boot | refused | `test_a_token_from_an_earlier_boot_is_no_proof` |
| policy says `sandbox = false`; macOS; the resident gate's child | refused | `test_no_wall_in_policy_refuses`, `test_macos_is_refused_until_its_profile_is_run`, `test_the_resident_gate_child_is_never_a_person` |

Not tested, so stated as open: `systemd-run --user` and `tmux new-window` start a process from another daemon, which
may sit outside the launch wall's mount namespace; if it does it can read the token. The e2e case does not cover them.
Also open: the policy's `sandbox = true` is intent, not fact. A session launched with `--no-sandbox` under a policy
that says on can read the token, and ws does not yet stamp "walled" on the identity record to refuse while such a
session is live (it needs the launcher, a follow-up with C111). The token's folder must exist when a runner starts for
the wall to hide it, so `ws msg person-token` refuses to make that folder; the install makes it.

### Confirmation

`tests/test_msg_person.py` (the person path and who asked),
`tests/test_msg.py` (the walled-class rules, the canary read of the whole shared file, export, forged senders and class
headers, replay, the caps, the typed store failures, retention, deletion), `tests/test_session_registry.py` (the runner
stamp and `resolve_caller`, including a real orphaned process) and `tests/test_mcp_server.py` (the tools and the bounded
frames).
