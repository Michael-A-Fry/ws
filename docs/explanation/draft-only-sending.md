# Draft-only sending

Draft-only sending is the rule that an agent prepares messages for you and never sends one itself.

**ws doesn't enforce this rule in v0.1.** This page says what exists, what doesn't, and what to do until it does.

## The rule

An agent may write a draft reply, a summary or a report. A person reads it and sends it. The agent has no send action.

## Why it exists

Sending is an act on the outside world and can't be taken back. A draft costs nothing if it's wrong. Draft-only
sending keeps the useful part of an assistant (the first draft) and drops the part that goes wrong in public.

## What exists today

- **The dial.** `dial` in `policy.toml` is `prepare`, `allowlist` or `act`. `prepare` means drafts only, no acts. It's
  the only value that `ws install`, `ws setup` and `ws gate init` ever write. A test pins this.
- **Source acts.** A source can list `acts` (for example `draft`) and `outward` acts (for example `send`). The default
  policy marks the mail source with `acts = ["draft"]` and `outward = ["send"]`, in mode `read`.
- **The Rule of Two reads the dial.** An outward act counts as a way out only when the source is in mode `act` and the
  dial isn't `prepare`. See [The Rule of Two](rule-of-two.md).
- **The run record.** Every run record stores the dial the run used (`policy.dial`).
- **The gate.** You can deny or ask about any command that sends. See [The gate](gate.md) and
  [Approvals](approvals.md).

## What doesn't exist

- ws ships no mail connector. No code in ws reads or writes mail, and no mail plugin is listed in `ws plugin list` or
  the setup screens.
- ws doesn't stop a tool you add yourself (an MCP server for your mail, a script) from sending. The dial is a setting
  that ws records and reads in the Rule of Two. It doesn't block a send.
- There is no `deny_send` setting and no check that a connector has no send scope.

## What to do until it exists

The how-to [Send drafts only](../how-to/send-drafts-only.md) lists the settings that exist: keep the dial at `prepare`,
keep mail in mode `read`, deny the commands that send, deny the web tools, and approve any push by hand. These reduce the
risk. They don't make it zero. If you add a mail connector, give it a draft-only scope at the mail provider, because that's
the only place a send can be stopped for certain today.

This gap is in [Known limits](known-limits.md).
