# Approve a command

Let an agent run a command only after you say yes to that exact call. To understand the rule first, read
[Approvals](../explanation/approvals.md).

## Before you start

- A policy and generated runner files: [Use the gate](use-the-gate.md).
- A terminal of your own, separate from the AI session.

## Steps

1. Mark the command in `policy.toml`. Add a pattern to `ask_commands`.

   ```toml
   [gate]
   ask_commands = ["git push*", "terraform apply*"]
   ```

   The patterns work like `deny_commands`. A command that also matches `deny_commands` stays denied.

2. Generate the runner files again, for each runner, and start a new session.

   ```sh
   ws gate generate --runner claude-code --out ~/.ws/gate/claude-code
   ```

3. Let the session try the command. The gate refuses it and prints an id.

   ```text
   ws gate: blocked: needs a person's approval: in a terminal of your own run `ws approve bd49118c36cd`, then retry the
   identical call (good for one use, within 10 minutes)
   ```

4. In your own terminal, list what's waiting.

   ```sh
   ws approve --list
   ```

   Each line shows the id, the time left, the runner and the command.

5. Approve the call. ws shows the exact call and asks.

   ```sh
   ws approve bd49118c36cd
   ```

   ```text
   Approve this call, once?
     id      bd49118c36cd...
     runner  "claude-code"
     kind    "command"
     call    "terraform apply"
     cwd     "/home/you/project"
     session "..."
   approve? [y/N]
   ```

   Read the call. Answer `y` to approve, or anything else to refuse. You can type the first 8 or more digits of the id.

6. Tell the session to retry the identical call. It runs.

## Success

Step 5 prints `ws: approved bd49118c36cd for one use, within 10 minutes; the session may retry the call`. The retry runs
once. A second retry asks again, with the same id, until you approve again.

## Things to know

- The approval is bound to the exact command, folder, session and runner. Change one byte and you need a new approval.
- An approval lasts 10 minutes and works once.
- `ws approve` refuses to run from a script or from inside a gated session. It needs a terminal. `--yes` skips the
  question but still needs a terminal.
- You can't approve a command that `deny_commands` also matches. Remove the deny rule first, on purpose.
- To refuse, do nothing. A pending request expires after 10 minutes.
