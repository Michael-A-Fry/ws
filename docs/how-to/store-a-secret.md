# Store a secret

Put an API key where ws can use it for a job and an AI session can't read it. To understand the rule first, read
[The secrets broker](../explanation/broker.md).

You type the value yourself, in your own terminal. Never paste a key into an AI session, a prompt or a job file.

## Before you start

- ws installed and `ws init` done ([Install ws](install.md)).
- The key, and the host it belongs to (for example `api.anthropic.com`).

## Steps

1. Register the name. Add a table to `$WS_HOME/secrets.toml` (`~/.ws/secrets.toml` unless you set `WS_HOME`). The registry
   says where a name may be used. It holds no values.

   ```toml
   [anthropic]
   hosts = ["api.anthropic.com"]
   header = "x-api-key"
   purposes = ["run"]
   ```

   For a bearer token, use `header = "authorization"` and `format = "Bearer {value}"`. Leave `purposes` out to allow any.
   Keep `hosts` to the one host that needs the key.

2. Store the value. ws asks for it without showing it. It reads standard input instead when there is no terminal.

   ```sh
   ws secret set anthropic
   ```

3. Check that it's set. The list shows names, hosts, state and owner. It never shows a value.

   ```sh
   ws secret list
   ```

4. Name the secret in a job file, so `ws run` leases it from the broker. The file is `$WS_HOME/jobs/NAME.toml`.

   ```toml
   [[secrets]]
   name = "anthropic"
   host = "api.anthropic.com"
   purpose = "run"
   ```

   The runner then holds a placeholder key and a local address, never the value.

5. Optional, Linux with systemd: put the store behind its own system user. This prints the one-time `sudo` steps. Read
   them, then run them.

   ```sh
   ws secret wall
   ```

   Afterwards the store belongs to the `ws-broker` user and your agent's user can't read it. Set later values as that
   user: `ws secret set` prints the exact command. The broker's copy of the registry is owned by root, so a later change
   to `secrets.toml` needs the `install -o root` line that `ws secret wall` printed.

## Success

`ws secret list` shows the name with the state `set`. `ws doctor` shows WS104 `ok` after the wall (`warn` before it:
"same-user boundary"). A job that names the secret runs without the key appearing in its environment or its run record.

## Remove or replace

- Replace: run `ws secret set NAME` again.
- Remove the value: `ws secret rm NAME`. The registry entry stays.
- Rotate: make a new key at the provider, run `ws secret set NAME`, then revoke the old key at the provider.

## If it doesn't work

- *isn't in secrets.toml*: do step 1 first. A name must start with a letter and use letters, digits, `-` and `_`.
- *the value is empty* or *has a line break*: paste only the key, on one line.
- *the store belongs to another user*: you ran the wall. Use the command that ws prints.

On macOS and WSL2 the secret wall isn't built yet. See [Known limits](../explanation/known-limits.md).
