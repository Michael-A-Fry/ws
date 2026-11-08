# Messages

One message store lets a session leave a message for another, on any runner or provider, and lets a lead reach a session
that's busy. This page covers the store, the commands, the `ws mcp serve` tools, the rules and how a message reaches the
receiver. The reasons are in [the decision record](../decisions/0023-one-message-store-with-class-bodies-apart.md).
The folders are listed on [Stores](stores.md).

## Commands

| Command | What it does |
|---|---|
| `ws msg send SESSION [TEXT]` | Leaves a message. `TEXT` of `-` reads standard input. Options: `--kind`, `--reply-to ID`, `--thread NAME`, `--key KEY`, `--tier`, `--ack-required`. |
| `ws msg list` | Your messages, oldest first, as envelopes (no text). `--sent` lists the ones you sent, `--unread`, `--after ID`, `--limit N` (up to 200). |
| `ws msg read ID` | One message left for you, with its text, marked read. `--ack` acknowledges it too. |
| `ws msg export` | JSON lines of the messages you sent or were sent, to standard output or `--out PATH`. `--out` writes a new file (0600) and refuses a path that exists; the lines go to a temporary file first and are put in place when the export is complete. `--after ID`, `--limit N` (up to 100000). |
| `ws msg watch` | The envelope feed for you (who, what kind, how big, delivered or not; never a body), cleaned for a terminal. `--after SEQ` carries on from a cursor, `--position-now` lists nothing and prints the cursor for the end, `--limit N`. It says `cursor_expired` when retention removed rows past your cursor (`pruned_through_seq` is the last removed) and `cursor_ahead` for a cursor beyond the end. |
| `ws msg wait` | Blocks until a message that no hook has delivered lands for you, then prints the envelope line. `--timeout SECONDS` (at most 1800), `--session-id ID` where one runner hosts several sessions. `--session-hook` is what a Claude Code `asyncRewake` hook runs: exit 2 with the envelope on standard error when a message lands (that wakes an idle session), exit 0 in every other case (it gives up, delivery is off, another waiter already runs for the session, it can't tell who it is, the store fails, or the run is a one-shot headless `claude -p`), because a hook's exit 2 wakes the session. It waits up to 23 hours, and exits within a second of its runner going. |

Every command takes `--json`. `ws msg export` to standard output prints the lines themselves, without an envelope. The
`--session-hook` form of `ws msg wait` keeps its own output.

The `ws mcp serve` tools are `msg_send`, `msg_list`, `msg_read` and `msg_ack`. They run the same code. Only the named
arguments are read, so a `from` or `class` argument is ignored.

## Who is sending

The sender is never an argument. `ws session start` records the runner's process (its id and start time) in the session
record, and a command is that session when the runner is one of its ancestors. A process that isn't under a runner (one
that double-forked, or a person's own shell) isn't a session, so it can't send, list or read.
`ws msg export` is the same: with no session behind it, it's refused (`ws.msg.export` itself refuses a missing session
id, so no caller can ask it for the whole store), and a session's export has only the messages it sent or was sent,
never a walled body. A person's own export of the whole store arrives with the owner path.

Where it all lives isn't up to the caller either. The registry, the policy (the classes), the secrets registry (the
names a message may not carry) and `messages.db` are all found from the account's own home folder (`ws.session.trusted_env`,
from the system's user database), and nothing in the process's environment is read: not `WS_HOME`, `WS_*_DIR`, `XDG_*`
or `HOME`. A command that sets one, and makes its own records, policy and database in the folder it names, is
still the session named in the real registry, and writes nothing to the real store or to any repo. No function in
`ws.msg` takes an environment. A person whose ws folders are set by one of those variables is refused (`ws msg` can't
tell which session is calling) until ws can read the layout from the runner's own start-up environment.

## Kinds

| Kind | Who sends it |
|---|---|
| `note`, `ask`, `stop-and-hand-over` | any session that isn't walled; these are all a walled session may be sent |
| `ack`, `done`, `stop-and-hand-over:done`, `ask:pending`, `counts` | any session; all a walled or counts-only session may send, with no text |

## Rules

With no `[classes]` in the policy, any registered session may send any kind to any session, and bodies are kept in the
shared database.

With `[classes]`:

- The receiver's class is found from its working folder at send and again at read. A class a sender supplies is never
  read.
- The receiving class's `inbound` decides who may send to it: `off` (the default for a walled or counts-only class),
  `lead-only` (the default for a full class) or `allowlist` with `inbound_allow` naming sessions. A reply isn't let in
  because it's a reply.
- The one thing the `lead-only` default lets through is a fixed kind (`ack`, `done`, `stop-and-hand-over:done`,
  `ask:pending`, `counts`) sent to a lead in a full class, from any session, a worker or a walled one. It has no text, so
  nothing leaves a class, and it's how a session says it's done. `off` and `allowlist` are obeyed as written, and a fixed
  kind is never let into a walled class.
- A walled or counts-only session sends only the fixed kinds, with no text, no idempotency key and no thread name.
  `--reply-to` may name only a message it sent or was sent.
- A walled or counts-only session is sent only `note`, `ask` and `stop-and-hand-over`. A thread name can't be set on a
  message to it.
- The body of a message to such a session is kept only in `<repo root>/.ws/messages/<class>.db`, where the repo root is
  the nearest folder at or above the receiver's working folder that holds `.git`. It has to be inside the paths the
  class's `repos` cover. The folder holds a `.gitignore` that ignores everything in it. The shared database has the
  envelope only.
- A body is read only by its recipient, and a walled body only by a recipient whose folder is in that class.
- Export never writes a walled body, for anyone.
- Retention is the receiving class's `retention_days` (30 with no class). Pruning runs at most once an hour, on the next
  send or list. Deleting a walled repo deletes its bodies with it; `prune` then drops the envelope rows for that repo.
  A repo ws can't see (no permission, or hidden by the wall) isn't treated as deleted, and neither is one whose folder
  is missing while the folder above it's on another device than the repo was on when its first message was written
  (an unmounted drive leaves an empty mount point behind). `prune` reports those rows as `unconfirmed` and keeps them until
  retention takes them. A bit of a class store left behind because it couldn't be reached or written is remembered in
  the shared database (`tidy`) and removed on the next prune. The job is dropped only when the store, or the repo, is
  confirmed deleted by the same device check; a repo that may be on an unmounted drive keeps it until the drive is back.

## Caps

These are constants in `ws.msg`.

| Cap | Value |
|---|---|
| Body | 16 KiB (bytes) |
| Rate | 30 messages a minute for each sender, in one fixed minute |
| Reply depth (hop) | 8: a message is one deeper than the one it replies to |
| Store size | 256 MiB for the shared file and for each class store |
| List | 50 by default, 200 at most; export 10000 by default, 100000 at most |

A body that names a registered secret (the names in `secrets.toml`) or matches a secret rule is refused, and the
message says what it looked like and never the text. A registry that won't read refuses the send. Every rule has
bounded repeats, so a hostile body can't make the scan run long.

An idempotency key makes a send safe to repeat: the same sender and key to the same recipient again returns the first
message's id and stores nothing. The same key to another session is a new message. The key is kept as a hash of the
sender, the recipient and the key.

A body must be text that encodes as UTF-8: a lone surrogate, or bytes on standard input that aren't UTF-8, is a
`bad_input` error.

## Tables

The shared database is `<state>/msg/messages.db` (the folder 0700, the file 0600, WAL, `PRAGMA user_version` 2).

| Table | Holds |
|---|---|
| `messages` | `id`, `from`, `to`, `sent` (UTC) and `sent_ms`, `t_signal_ms`, `t_stored_ms`, `kind`, `tier`, `body` and `sha256` and `size_bytes` (non-walled receivers only), `idem`, `thread_id` (the id, as text, when none is given), `reply_to`, `hop`, `class`, `walled`, `store` (the repo root, walled receivers only), `has_body`, `store_dev` (the device the repo was on) |
| `recipients` | `message_id`, `recipient`, `read_ts`, `ack_ts`, `ack_required` |
| `envelope` | `seq`, `message_id`, `from`, `to`, `kind`, `sent`, `delivered_ts`, `class`, `size` (S up to 1 KiB, M up to 4 KiB, L over) |
| `meta` | `last_prune`, `pruned_through_seq` (the highest envelope `seq` retention has removed) |
| `dq_<hash>` | one delivery queue per receiver (`ws._vendor.sqlqueue`): `message_id`, `data` (the message id), `status`, `claim_id`, `lock_time`, `tries`; dropped once empty |
| `tidy` | `store`, `class`: class stores with bodies still to remove |

The viewer and `ws msg watch` read `envelope` only. A class store has one table, `bodies` (`message_id`, `to`, `sent_ms`,
`body`).

## How a message reaches the receiver

Switch on the `inbox` guard (see [Guards](guards.md#inbox)). Each receiver has a delivery queue in the same database, one
table (`dq_<hash of its id>`) filled in the same transaction as the message. At the receiver's turn the guard claims what
is waiting (`deliver`), stamps the envelope delivered and, for a message that needs no acknowledgement, finishes the claim.
One that does need it stays claimed until `ws msg read ID --ack`, and is offered again, with a new claim, after 15 minutes
(`LEASE_S`, longer than a normal turn). A message to a session that has ended stays pending. Only `urgent` and `info`
messages are handed over; `noise` is for the viewer.

| Runner | At a prompt and between steps | When the session is idle |
|---|---|---|
| Claude Code | A prompt hook's text; a warning after each tool call; an `urgent` message continues a stopping turn | `ws msg wait --session-hook` as an `asyncRewake` hook: exit 2 wakes the session with the envelope line |
| OpenCode | The prompt guards at each prompt, and again before each model request (`experimental.chat.messages.transform`) | The plugin runs the waiter and wakes the session with `client.session.promptAsync` |
| Another runner | Not built | `ws msg send` types a fixed line into the receiver's tmux pane when the receiver is idle: short text with `send-keys`, longer by paste buffer, then Enter on its own after 150 ms, and a turn that didn't start is sent Enter once more and then reported |

Whatever is handed over is text from another session: it is marked untrusted, cut to 4000 characters, cleaned for a
terminal and flagged when it reads like an instruction, and it grants no permission. A receiver in a walled class is never
handed a body by a hook or a wake: it gets how many are waiting and reads each with `ws msg read`. Only a wake signal and
an envelope cross, in `ws msg wait`, `ws msg watch` and the reminder alike.

**Whose hook is it.** A body goes out only when the hook process descends from the runner of the session the payload
names (`ws.session.resolve_caller`). Through the resident gate the process is the one the kernel reports on the hook's
connection. Where the system reports none (macOS) no body goes through the gate: the session gets the body-free reminder
(at most once in two minutes after a call) and reads with `ws msg read`. A forged or replayed event for another session
reads no body and marks nothing delivered.

**Inside the OS wall.** Under `ws gate launch` the state folder is read-only and the hook runs inside it. The session has
no identity there (it can't be written), so nothing is handed over. The guard tries to open the store read-only for the
count; a WAL database in a read-only folder can't be opened (SQLite needs to make its `-shm` file), which is what is seen
there, so the session is told once, in one body-free line, that messages can't be read from here. Where the store does open
(its WAL files are already there and readable) the session is given how many are waiting, without who from. Delivery of
bodies and the idle wake are outside the OS wall only; no folder was made writable for them.

## Errors

Each is a typed error with a hint. `unavailable` carries `details.reason`: `locked`, `full`, `read-only`, `unopenable`,
`unreadable` or `damaged`, taken from SQLite's error code, never from its text. A send writes the message, its recipient
row, its body and its envelope in one transaction, so a failure leaves none of them.

| Kind | When |
|---|---|
| `refused` | no session is calling; the policy or the secrets registry won't read; the class rules above |
| `not_found` | no such session, or no such message for you |
| `bad_input` | a malformed argument, or a body that isn't valid UTF-8 text |
| `exists` | `export --out` named a path that's already there |
| `too_large`, `rate_limited`, `hop_limit`, `secret_refused` | the caps above |
| `unavailable` | the store is locked, full, read-only, can't be opened or is damaged |

Inside the wall the state folder is read-only, so a send from there is `unavailable` (`read-only`). Delivery to a
session inside the wall is limited to the reminder, as above.

## For other modules

`ws.msg` has `send`, `inbox`, `sent`, `read`, `ack`, `mark_delivered`, `envelopes`, `export` and `prune`, and for the
delivery guard and the viewer `deliver`, `pending`, `handover_delivered`, `last_delivered_outbound`, `receiver_walled`,
`watch`, `wait`, `wake_pane` and `clean`. Only `send` finds
its own caller (`caller()`); the others take the session id, which a command gets from `session.resolve_caller` and a hook
gets from its payload. Functions take `now` (the clock) where time matters, so tests can inject it.
