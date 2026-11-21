---
status: accepted
date: 2026-11-21
decision-makers: maintainer
---

# A setup step only a person can do is data, walked by one engine

## Context and Problem Statement

Every integration and some features need a person to act: make a bot, approve a sign-in, paste a token, press Start.
Each agent that builds one improvises the steps, so each integration looks and behaves differently, and a secret can
end up in a chat or a log. How do these steps stay the same everywhere, and give an agent one contract to build against?

## Considered Options

- Leave each integration to write its own prompts and checks.
- A plug-in framework that integrations extend with code.
- One declarative file per integration, a closed set of step types, and one engine that walks, shows and undoes them.

## Decision Outcome

Chosen option: the declarative file and one engine. A flow is TOML text, held as a string in
`src/ws/connect/flows/<id>.py` (schema `ws.connect-flow/1`, JSON Schema in docs/reference/connect-flow.schema.json) and
`ws connect` is the command.
This is a format, not a plug-in framework: the step types are a closed set, flows are found only in that folder, and a
third party can't add one. It's not a stable extension point before 1.0, and the core stays standard-library only
([0018](0018-core-is-standard-library-only.md)).

Each rule and where it comes from:

| Rule | Source |
|---|---|
| A flow is a list of declarative steps run by a stateful engine; a step is a form, a hand-off to another site, or a check. | Home Assistant config flow |
| Every flow names a stable `unique_key` (a bot id, never a host, address or display name); nothing counts as configured until a real call (`verify`) returns an identity label and that id. | Home Assistant config flow (unique id); Zapier's test-auth call that labels a connection |
| A `link` step is a prefilled URL with a one-time `nonce` and an expiry, and the next `verify` proves the person used it. | Telegram `t.me/<bot>?start=<nonce>` (a 64-character `A-Za-z0-9_-` parameter binds a chat); GitHub App manifest and Slack manifests (prefilled create links with a `state`) |
| A `capture` field marked `secret` is read with no echo, held in memory until its verify passes, then goes to the secrets store and nowhere else; a failed verify keeps the old secret. | Credentials with a test call in n8n and Zapier; ws's own secrets rules |
| Each step may declare `revoke`: delete the secret, tell the person what to undo on the other side, or forget what was learned. | Home Assistant's remove-an-entry; the revoke commands of the services the flows connect |
| An agent runs `--dry-run --json` and sees each step as `done` or `needs_human`, with the link and its expiry. It never sees a secret. | The agent contract this record sets; Home Assistant's redacted diagnostics for `--diagnose` |

The first flow is Telegram: info and a link to BotFather, capture the bot token, verify with `getMe` (identity is the
bot's username, unique id the bot's id), then a link `https://t.me/<bot>?start=<nonce>` and a verify that the nonce
arrived (it binds the chat). Revoke deletes the secret and tells the person to send `/revoke` to BotFather.

Rules that follow from where the secret goes:

- The verify call runs only in the person's own terminal, in the engine's process, never in an agent's. A Telegram token
  sits in the URL path, which the broker's header injection can't carry, so the broker isn't the route here.
- The flow declares a `[secret]` block. `ws secret ask` refuses names the registry doesn't list, and the registry is the
  person's own, so `ws connect` prints the one registry step for the person and never edits it. A `{secret}` is sent only
  to a host the `[secret]` block names (the loader enforces it), over https, with no redirect followed.
- The walk and `--revoke` refuse inside an agent session (the check the other person-only commands use,
  [0020](0020-admin-commands-refuse-inside-an-agent-session.md)). The list, `--dry-run` and `--diagnose` change
  nothing and are open to an agent.
- The agent contract has a frozen `schema_version` (1), per-step `status` and `action`, `expires_at`, and exit codes 0
  (done), 3 (needs a person) and 1 (error). Exit 3 is also what a refusal returns; the JSON `status` or `error_kind`
  says which.

Left out of this phase, each a row to pick up later: the device step (RFC 8628: polling with `interval`, `slow_down`,
expiry, and the code and text URL beside any QR); the `callback` step (server-side code exchange, GitHub manifest
style, so the person never copies a secret); `--reauth` (re-run capture and verify, abort when the unique id differs);
a QR code (a QR encoder is its own task in a standard-library-only core); the phone page and Telegram as places to
capture (today's capture needs a tmux session). Managed Bots, where Telegram creates the bot from a prefilled link and
no token is pasted, is the upgrade path for the Telegram flow once ws can hold a manager bot.

### Consequences

- Good: a new integration is one file and the same steps each time; an agent builds against one contract.
- Good: a secret has one path in (the person's terminal, to the store) and no copy in state, logs or JSON.
- Bad: a flow that needs code (a callback, a device poll) waits for the engine to grow those step types.
- Bad: a shipped flow is TOML text inside a Python string, because the secret wall copies only `.py` files and its
  tree check refuses any other file in the package. A flow outside the package is a plain `.toml` file.

### Confirmation

`tests/test_connect.py` walks a fake Telegram API from first step to done and checks that the secret is in the store and
in no state file, output or error; that a failed verify stores nothing and keeps an old secret; that revoke then a
re-walk works; that `--dry-run --json` lists the steps with their exit codes; that the walk and `--revoke` are refused
in an agent session; and that the loader and the JSON Schema refuse a flow that sends `{secret}` to an unnamed host.
