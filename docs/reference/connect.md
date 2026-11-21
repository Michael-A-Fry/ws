# Connect flows

The format of a connect flow, the state it keeps, and the JSON an agent reads. The reasons are in
[0033](../decisions/0033-a-setup-step-is-data-walked-by-one-engine.md); the steps to add one are in
[Add a connect flow](../how-to/add-a-connect-flow.md). The file format is checked by
[connect-flow.schema.json](connect-flow.schema.json) and, for what a schema can't say, by `ws.connect.spec`.

A flow is a format, not a plug-in framework: the step types are a closed set, flows live only in `src/ws/connect/flows/`,
and a third party can't add one. It isn't a stable extension point before 1.0.

## The file

`src/ws/connect/flows/<id>.toml`, named for its `id`. `_template.toml` is the starting point and isn't listed.

| Key | Meaning |
|---|---|
| `schema` | `"ws.connect-flow/1"` |
| `id` | Lower-case letters, digits and `-`, at most 32. `list` is reserved. |
| `title` | What the person sees first. |
| `unique_key` | The name the stable id from the verify is kept under (`bot_id`). Never a host, address or display name. |
| `[secret]` | `name` (the registry name) and `hosts` (where a `{secret}` may be sent). Left out when the flow stores no secret. |
| `[[steps]]` | The steps, in order. |

## Steps

Every step has `id` (unique in the flow) and `type`; `title` and `revoke` are optional.

| `type` | Keys | What happens |
|---|---|---|
| `info` | `text` | Shown; the person confirms they've read it. |
| `link` | `url` (https), `text`, and for a one-time link `nonce = true` and `expires_s` (60 to 86400) | The URL is shown for the person to open. With `nonce`, `{nonce}` in the URL is a fresh 43-character `A-Za-z0-9_-` value that's kept until `expires_s` runs out; an expired one is replaced on the next walk. |
| `capture` | `fields`: each `name`, `label`, optional `secret = true` and `pattern` (a regular expression the whole value must match) | A secret field is read with no echo and held in memory. A capture with a secret field must be followed at once by a `verify` with `identity` and `unique_id`: the secret is stored only when that passes. At most one secret field per flow. |
| `verify` | `request.url` (https), then either `identity` and `unique_id` (paths into the JSON answer), or `find` | A real call. `identity` and `unique_id` give a label and a stable id. `find` looks in the list at `find.list` for the first item whose `where` paths equal the given texts and keeps the `take` paths (the Telegram flow reads the chat id of the message that carries the nonce). Success is HTTP 200 and no `"ok": false`. |

Placeholders in a URL or a `where` text: `{secret}` (in a `verify` URL only, and only to a host in `[secret].hosts`),
`{nonce}`, `{identity}`, the `unique_key`, a non-secret field name, and a `take` name. The host part of a URL is fixed
text. A request is a GET over https and follows no redirect.

`revoke`: `action = "delete_secret"` (needs `[secret]`), `action = "tell"` with `text` (what the person does
elsewhere), or `action = "forget"` (clears what the flow learned). `--revoke` runs them last step first, then clears
the state.

Not in this phase: `device` (RFC 8628), `callback`, `--reauth`, QR codes. The loader refuses a step type it doesn't know.

## State

`<state>/connect/<id>.json`, 0600 in a 0700 folder, schema `ws.connect-state/1`: see [Stores](stores.md#record-files).
It holds step status, the learned identity, unique id and bound chat id, and the current nonce. It never holds a secret:
a captured secret goes from the prompt to the secrets store only. `--diagnose` shows it with the nonce redacted.

## The agent contract

`ws connect <id> --dry-run --json` reads state and the registry and runs nothing. Its envelope is `ws.cli/1`; `data` is
frozen at `schema_version` 1. A key may be added; none is removed or changed in meaning without a new number.

| Key | Meaning |
|---|---|
| `schema_version` | `1` |
| `contract` | `"ws.connect/1"` |
| `flow`, `title` | The flow |
| `status` | `done` when every step is done and nothing blocks, else `needs_human` |
| `exit_code` | `0` for `done`, `3` for `needs_human`; the process exits with it |
| `next_step` | The id of the first step not done, or `null` |
| `run` | The command the person runs: `ws connect <id>` |
| `blockers` | When present, each `kind` and `action`. `secret_not_registered` carries the registry entry for the person to add |
| `steps[]` | `id`, `type`, `status` (`done` or `needs_human`), and when not done `action` |

A step also carries what its type needs: `title`, `text` (info, link), `url` or `url_template` (link; the template when
a placeholder isn't known yet), `expires_in_s` and, once a nonce exists, `expires_at` (a nonce link), and `fields`
(capture: each `name`, `label`, `secret`). `action` is one of `read`, `open_link`, `paste_secret`, `enter_value`,
`run_verify`. A verify's request URL is never shown.

Exit codes of the command: 0 done, 3 needs a person (or a refusal: the envelope's `error_kind` is `refused`), 1 an error
(a flow that doesn't exist, a failed verify, a damaged state file), 2 usage.

## Who may run what

| Command | In an agent session |
|---|---|
| `ws connect` (list), `--dry-run`, `--diagnose` | Allowed: they change nothing |
| `ws connect <id>` (the walk), `--revoke` | Refused, exit 3: they run in your own terminal |

## Rollback

A verify that fails stores nothing and leaves an existing secret as it was. `--revoke` then a new walk starts clean.
