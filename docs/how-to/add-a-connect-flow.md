# Add a connect flow

Use this for every integration or feature that needs a person to do something: make an account or bot, paste a token,
approve a sign-in, press Start. Don't write prompts of your own; describe the steps in a flow file and the engine walks
them the same way each time. The format is in [Connect flows](../reference/connect.md).

## Steps

1. Copy `src/ws/connect/flows/_template.py` to `src/ws/connect/flows/<id>.py` (a `-` in the id is `_` in the file
   name). The TOML sits in `TOML = """..."""`: ws ships flows as Python because the secret wall copies only `.py`
   files.
2. Write the steps in the order a person does them, using only `info`, `link`, `capture` and `verify`.
   - Say what to do in one or two plain sentences (`info`), then give the page as a `link`.
   - Ask for a secret with `capture` and `secret = true`, with a `pattern` so a wrong paste is caught before any call.
   - Follow it with a `verify`: one real call that returns an `identity` (a label to show) and a `unique_id` (a stable id,
     never a host, address or display name). The secret is stored only if this passes.
   - To bind a person's account or chat, use a `link` with `nonce = true` and `expires_s`, then a `verify` with `find`.
3. Add a `[secret]` block with the registry name and the hosts the secret may be sent to. A `{secret}` goes to nothing
   else.
4. Give each step that leaves something behind a `revoke`: `delete_secret` for the stored secret, `tell` for what the
   person must undo on the other side.
5. Check it:
   - `uv run --locked pytest -q tests/test_connect.py` (every shipped flow is checked against the JSON Schema and the
     loader).
   - `ws connect <id> --dry-run --json` shows each step as `needs_human` and exits 3.
6. Test the walk against a fake of the service, as `tests/test_connect.py` does for Telegram. No test calls the real
   service, and the secret in a test is built at run time.

## Rules

- A secret never goes in a chat, a prompt, a log, the state file or the JSON. The engine reads it with no echo and puts
  it in the secrets store; keep it that way.
- The person adds the registry entry (`ws connect <id>` prints the lines to add); a flow never edits the registry.
- An agent runs `ws connect <id> --dry-run --json` and passes the `needs_human` steps (link, expiry) to the person. It
  doesn't run the walk or `--revoke`: they refuse inside an agent session.
- Keep a flow generic. Anything one person needs belongs in their own config, not in a flow that ships with ws.
- A step type that doesn't exist (device, callback) isn't added in a flow file: the engine grows it first, with its
  tests and a record in `docs/decisions/`.
