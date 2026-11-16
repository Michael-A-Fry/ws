# Store a secret

Put an API key where ws can use it for a job and an AI session can't read it. To understand the rule first, read
[The secrets broker](../explanation/broker.md).

You type the value yourself, in your own terminal. Never paste a key into an AI session, a prompt or a job file.

## Before you start

- ws installed and `ws init` done ([Install ws](install.md)).
- The key, and the host it belongs to (for example `api.anthropic.com`).

## Steps

1. Register the name. Add a table to `~/.config/ws/secrets.toml` (`secrets.toml` in [the config folder](../reference/stores.md#where-ws-keeps-its-files)). The registry
   says where a name may be used. It holds no values.

   ```toml
   [anthropic]
   hosts = ["api.anthropic.com"]
   header = "x-api-key"
   purposes = ["run"]
   ```

   For a bearer token, use `header = "authorization"` and `format = "Bearer {value}"`. Leave `purposes` out to allow any.
   Keep `hosts` to the one host that needs the key. `paths = ["/v1/messages"]` limits the broker to those request paths
   (a prefix on a folder boundary; anything else gets a 403 and never reaches the host).
   With `paths` set, a request path may hold only letters, digits and `._~/-`, and the list can't be empty. A change to
   `paths` or `ttl` bites open leases too. `ttl = 600` ends this name's
   leases after 600 seconds (an hour is the default, a day the most). To lease it only to runs that declare it, add a
   `tier` and an `area` ([Use short-lived credentials](use-short-lived-credentials.md)). An unknown key in a table refuses the whole
   registry, so a typo never leaves a name unscoped.
   The broker reads the registry only if it's yours and closed to group and world writes. If an editor or a umask of
   002 left it `0664`, run `chmod go-w ~/.config/ws/secrets.toml` (the broker answers `registry_untrusted` until then).
   The same goes for its folder. A symlinked registry is refused as well: keep the file itself in the config folder.

2. Store the value. ws asks for it without showing it. It reads standard input instead when there is no terminal.

   ```sh
   ws secret set anthropic
   ```

   To paste it into its own window instead, from inside tmux: `ws secret ask anthropic` opens a window with a prompt that
   shows nothing as you paste, and `ws secret wait anthropic` blocks until it's stored. The value never goes through
   a command line, the environment, a log or the reply. An AI session may run both, because neither shows a value;
   the typing happens in the window, and the window names the store it will write to. Only you can use `set`,
   `import`, `rm` and the window's own prompt. The window starts with an empty environment and your account's home
   folder only, so a session can't steer where the value goes: `ask` refuses when `WS_HOME`, `HOME` or an `XDG_*`
   variable points ws's folders anywhere but your account's own. The value goes to the store your account's profile
   names (`[roles.secrets] store`), or the default one; the window prints which before you type.
   The older `ws-secret ask NAME` and `ws-secret wait NAME` still work (the same commands);
   plan to move to `ws secret` before it's removed, 30 days after AI_Workspace is switched off.

3. Check that it's set. The list shows names, hosts, state and owner. It never shows a value.

   ```sh
   ws secret list
   ```

4. Name the secret in a job file, so `ws run` leases it from the broker. The file is `~/.config/ws/jobs/NAME.toml`.

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

## A value ws makes itself

Some secrets are only ever needed by ws, such as the backup password restic uses. Nobody needs to type or paste those.
Mark the name `source = "generate"` (it needs no hosts) and let ws make it:

```toml
[restic-password]
source = "generate"
```

```sh
ws secret generate restic-password
```

The value is 256 random bits, written atomically with mode 0600 and never printed. A second run refuses, because a new
value would lock you out of what the old one protects; `--replace` makes one anyway.

You still want a copy somewhere safe, for a restore on a new machine. `ws secret reveal restic-password` prints a link
to a page that shows it once, on your own device:

- the page listens on this machine's one Tailscale address, and only a device logged in as `owner` (set it in
  `[roles.secrets]` of your profile, for example `owner = "name@"`; there is no default) with no tags gets it;
- opening the link shows a **Show** button, not the value, so a link preview can't use it up;
- after **Show**, the page is closed for good, nothing is cached and nothing is logged;
- an unused link stops working after 15 minutes. `--again` shows a value a second time.

An AI session may run exactly two of these for you, so you never type them: `ws secret generate NAME` (never with
`--replace`) and `ws secret reveal NAME` (with `--again` or `--minutes N` at most, and only on the tailnet). Neither lets
the session see a value: the page refuses this machine's own addresses. Every other `ws secret` command stays yours, a
walled class's session gets neither, and each generate and reveal writes a line (who, which name, when; never the
value) to `logs/secrets-audit.jsonl` in the state folder.

## Success

`ws secret list` shows the name with the state `set`. `ws doctor` shows WS104 `ok` after the wall (`warn` before it:
"same-user boundary"). A job that names the secret runs without the key appearing in its environment or its run record.

## Check them on a schedule

A weekly job that has the runner read `ws doctor` and say what's wrong with the store, never a value. Save it as
`~/.config/ws/jobs/secrets-audit.toml`, then `ws schedule apply`:

```toml
runner = "claude-code"
model = "claude-haiku-4-5"
prompt = """
Run `ws doctor --offline`. Report only the secrets checks (WS104 and neighbours) that are not ok,
one line each. Never print or open a secret value.
"""
max_turns = 4
budget_usd = 0.25
schedule = "weekly mon 09:00"
timeout_s = 600
on_failure = "alert"
log = "file"
uninstall = "ws schedule remove secrets-audit"
expected_rate = "7d"
data_class = "none"
```

## Remove or replace

- Replace: run `ws secret set NAME` again.
- Remove the value: `ws secret rm NAME`. The registry entry stays.
- Rotate: make a new key at the provider, run `ws secret set NAME`, then revoke the old key at the provider.

## If it doesn't work

- *isn't in secrets.toml*: do step 1 first. A name must start with a letter and use letters, digits, `-` and `_`.
- *the value is empty* or *has a line break*: paste only the key, on one line.
- *the store belongs to another user*: you ran the wall. Use the command that ws prints.

On macOS and WSL2 the secret wall isn't built yet. See [Known limits](../explanation/known-limits.md).
