# What a hand-built hook set does, and where ws does it

People who've wired their own hooks into Claude Code or OpenCode usually end up with the same kinds of hook: a shell
guard, a secrets guard, a protected-files guard, a staging guard, a claims check, an alert. This page maps each kind to
the ws rule or guard that does the same job. It's for anyone moving a hand-built set to ws, and for anyone who wants to
know what ws covers without reading the code.

Each row is one hook. The status says where its job lives now:

- **covered**: a ws rule or guard does it. The last column names the test that sends the same sample call to the ws
  side and checks the decision.
- **estate (personal)**: the job belongs to one person's own setup (their dashboards, their folder layout, their
  queue), so it lives in that person's private config and not in ws. ws gives the place to hang it (a `deny_commands`
  pattern, a guard, a hook of your own), and the reason is in the row.
- **dropped (reason: ...)**: ws has no counterpart, and the reason says why.

All of ws's hooks reach the runner through one command, the [resident gate](gate.md#the-resident-gate), so each
covered row runs in the same fixed order: the policy's rules first, then the guards that are on, lowest priority first
(see [guards](guards.md#the-order-guards-run-in)).

| # | What the hook does | In ws | Status | Test |
|---|---|---|---|---|
| 1 | Blocks irreversible shell commands: recursive deletes of system folders, forced pushes, disk formatting | `[gate] deny_commands` (the default list covers these; add your own patterns) | covered | `test_parity_irreversible_shell_commands` |
| 2 | Blocks reads of credential files and key stores, by a shell command or by a file tool | `[gate] deny_read`, applied to shell words, Read, Grep and Glob alike | covered | `test_parity_credential_reads` |
| 3 | Stops one session sending keystrokes to, or killing, another terminal-multiplexer session | A `deny_commands` pattern you add (such as `tmux kill-*`); which sessions to protect depends on how you work | estate (personal) | none: row 1 shows the mechanism |
| 4 | Keeps a session out of folders sealed off for a class of repository (reads and writes) | `[gate] deny_read` and `allow_write`, and the sandbox's `denyRead`; the list of sealed folders is yours | covered | `test_parity_sealed_folders` |
| 5 | Lets the files that hold the guardrails be edited only by the session that owns them | ws protects its own policy, settings, hooks and folders from the edit tools and from shell redirects | covered | `test_parity_guardrail_files` |
| 6 | Blocks staging the whole tree (`git add -A`, `git add .`, `git commit -a`) | the `bulk-staging` guard | covered | `test_parity_bulk_staging` |
| 7 | Checks a claim that a commit is merged or pushed before the message goes | the `claims` guard | covered | `test_parity_claims` |
| 8 | Tells a new session which state and hand-off files exist | the `session-context` guard | covered | `test_parity_session_context` |
| 9 | Makes a session load a skill before it writes certain files | the `skill-gate` guard | covered | `test_parity_skill_gate` |
| 10 | Caps how many subagents run at once | the `spawn-cap` guard | covered | `test_parity_spawn_cap` |
| 11 | Stops a conductor resuming a subagent that has grown past a token limit or been resumed twice | none | dropped (reason: no replay row or incident) | none |
| 12 | Stops a session making tests pass by skipping or weakening them | the `test-weakening` guard | covered | `test_parity_test_weakening` |
| 13 | Refuses to launch isolated agents or workflows from anywhere but a repository's main checkout | none; it depends on the workflow tool and the folder layout in use | estate (personal) | none |
| 14 | Checks that a workflow definition is current before it runs | none; it belongs to one workflow tool | estate (personal) | none |
| 15 | Pushes a phone notification when a session needs input or finishes | `ws notify alert`, written into the Notification and Stop hooks by `ws install` | covered | `test_parity_phone_alert` |
| 16 | Puts a permission prompt in a personal queue and closes it when answered | none; the queue is one person's tool (the alert in row 15 still fires) | estate (personal) | none |
| 17 | Sends session events to a personal dashboard | none; the dashboard is one person's | estate (personal) | none |
| 18 | Captures corrections the person makes, for later review | none; the review process is one person's | estate (personal) | none |
| 19 | Detects trigger phrases in a prompt and routes them | none; the phrases and routes are one person's | estate (personal) | none |
| 20 | Nudges after an incident | none; it names one person's incident process | estate (personal) | none |
| 21 | Logs token usage for a personal dashboard | none; `ws run` writes a [run record](stores.md) for each headless job, and interactive usage stays in the runner's own transcript | estate (personal) | none |
| 22 | Draws a custom status line | none; the status line is the runner's own setting | estate (personal) | none |

The ws guards with no row above are ones nobody asked for in a hand-built set: `syntax-check` (a syntax check after a
write), `dev-server` (stops a second dev server starting), `checkout-pin` (keeps work in worktrees) and `record-calls`
(records calls for [replay](gate.md)). They're off or on in `[guards] on`, like the rest.

## How the rows are kept true

`tests/test_hook_parity.py` reads this table. A `covered` row must name a test in that file, and each test sends one
sample call that a hand-built hook would stop (and one it would let through) to the ws side. The test fails if a row is
added without one, if a named test is missing, or if the table's count of rows changes without the page.

A row can move from `estate (personal)` or `dropped` to `covered` when ws grows the rule. Moving the other way needs a
reason in the row.
