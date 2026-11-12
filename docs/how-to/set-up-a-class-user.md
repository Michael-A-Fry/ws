# Set up a class user

Give a data class its own operating-system account, so a session of that class runs as that account and can't read your
home folder or another class's. ws prints the commands and the file; you run them.

A walled class is optional. It needs Linux, WSL2 or macOS and a second account on the machine. Without one, the
[gate](../explanation/gate.md) and the [wall](../explanation/wall.md) still work as before.

## Before you start

- Linux, WSL2 or macOS, and an account that can use `sudo`.
- A class in `policy.toml` that names the account and a repo folder it works in. `ws walls init NAME` prints one for you
  to read, and `ws walls init --write NAME` appends it to your policy (backed up first, the whole file checked
  afterwards). It writes no dial and sets nothing to `act`. See [`[classes.NAME]`](../reference/policy.md).

  ```toml
  [classes.work]
  access = "walled"
  os_user = "ws-work"
  repos = ["/srv/work/**"]
  owner = "Your name"
  review_date = 2027-03-01
  ```

  A repo is a folder, or a folder with a trailing `/**`. A wildcard in a folder name (`/srv/work/ops-*`) is refused with
  a usage error, because the file and the `chown` would then cover the folder above the repos; list each repo instead.
  Use a folder the account can reach. A `~` in a repo means the account's home (`/home/NAME`, or `/Users/NAME` on
  macOS), whether or not the account exists yet; a folder under your own home is closed to the account by step 3.
  `inbound` stays `off` for a walled class unless you set it, so nobody can send a message in. - Claude Code with the
  sandbox (bubblewrap and socat on Linux). See [Use the wall](use-the-wall.md).

## Steps

1. Ask ws what to run. It reads the policy and the system's user list, writes nothing and creates no user.

   ```sh
   ws install --class work
   ```

   An unknown class, or a class with no `os_user`, is a usage error (exit 2) that says what to add.

2. Read what it printed: two files.

   The managed-settings file goes in Claude Code's managed-settings folder, which Claude Code reads for every account on
   the machine, so a bare `claude` run as the class account is bound by it. It requires the sandbox, turns off the
   unsandboxed retry and bypass mode, lets sessions write only in the class's repos and the scratch folder, hides the
   home folder from them except the repos, keeps the network to the class's hosts, and records that only the lead may
   send in.

   The launch file is read by `ws gate launch`, not by Claude Code: it sits in a `ws-classes` folder beside the
   managed-settings folder, which Claude Code doesn't read, and binds sessions of this class only. It denies `Read` and
   `Edit` of the home folder, ws's own folders and the other classes' repos, holds file reads to the working folders,
   refuses messages from other sessions (`crossSessionInbound`, and `SendMessage` and `ListAgents` denied), keeps
   `disableAllHooks` false and OpenTelemetry prompt and tool logging off, and holds the network to the class's hosts.

3. Run the commands it printed, in order: create the account and close its home folder, close your own home folder
   (`chmod 700 "$HOME"`), give the account the repos, then place the two files. The managed-settings file goes in
   `/etc/claude-code/managed-settings.d` on Linux (the managed-settings folder under `/Library/Application
   Support/ClaudeCode` on macOS) as `50-ws-class-NAME.json`. The launch file goes in `/etc/claude-code/ws-classes` (the
   `ws-classes` folder beside it on macOS) as `NAME.json`. Both are owned by root, readable by everyone and writable by
   no one else, in a folder root owns. Each is written as `.json.tmp` first and moved into place, so a paste that stops
   half way leaves nothing ws or Claude Code would read.

4. Give the account its own ws, policy and Claude Code. The ws under your home is out of its reach once the home is
   closed, and its sessions read their own policy, so the class has to be in that one too.

   ```sh
   sudo -u ws-work -i
   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash
   ws setup
   ```

   Add the same `[classes.work]` table to that account's `policy.toml` (see [Install](install.md)), then run
   `ws gate generate --runner claude-code` as the account. Run `claude` once and sign in as that account.

5. Start a session of the class as that account, from a repo of the class.

   ```sh
   cd /srv/work/app
   ws gate launch --runner claude-code -- claude
   ```

   `ws gate launch` merges the class's launch file into the settings it passes to Claude Code. It refuses to start (exit
   3) when the launch file isn't in place or isn't root-owned (the message names `ws install --class NAME`), and when
   the project's `.mcp.json`, `.claude/settings.json` or `.claude/settings.local.json` holds anything outside an
   allow-list: a `.mcp.json` may start no MCP server, and a settings file may hold only `model`, `permissions` (`deny`,
   `ask` and a `defaultMode` of `plan` or `default`), `attribution`, `includeCoAuthoredBy`, `outputStyle`, `language`,
   `cleanupPeriodDays`, `respectGitignore`, a `sandbox` that doesn't widen the sandbox, an `env` of `NODE_ENV`, `TZ`,
   `LANG`, `LC_ALL`, `DISABLE_TELEMETRY`, `DISABLE_AUTOUPDATER`, `BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS` and
   `MAX_THINKING_TOKENS`, and the schema key. The message names the file and the key to delete.

6. Check it.

   ```sh
   ws walls check
   ws doctor
   ```

   `ws walls check` runs without sudo. It checks that each part's settings are in place: inbound is refused from outside
   the class, a sealed file can't be read or edited, one class's file isn't given to another class, the hook runs and
   denies under bypass and auto modes, a project that adds an MCP server or hook is refused, a project can't switch the
   hooks off or add plugins (the launch file sets `disableAllHooks` false), per-repo settings only narrow, telemetry
   content logging is off, the owner and review date are set, and `SendMessage` and `ListAgents` are denied. It exits 1
   when a case fails, or when the host isn't set up yet, and prints the next command.

## Success

`ws walls check` exits 0. `ws doctor` prints one WS014 line per part of the managed-settings file for the class, all
`ok`: the OS user exists, the file is present and root-owned, the sandbox is required, unsandboxed commands are off,
bypass is off, writes are limited, the home folder is hidden and inbound is lead-only. A part that isn't ok says which
one, and the `fix:` line says what to run. WS017 reminds you when the class's review date is due.

## If it refuses

- `ws gate launch` exits 3 for a session of the class started as any other account. The message names the class, not the
  account. ws reads the account from the system's user list, never from `$USER` or `$HOME`, and the class from where the
  session starts, so setting those changes nothing. Start it as the class's account. - WS014 `fail` on the file line:
  the file is a link, isn't owned by root, someone else can write it (or its folder), or the account can't read it (a
  file nobody else can read means its sessions run without the wall). Run the `fix:` line; it's the command for the
  problem the line names. - One class per machine is the tested shape (see [Known
  limits](../explanation/known-limits.md)). Each session is given its own class's launch file and no other. - The
  inbound row says the file records lead-only. That setting is a record; what lets a session send in is the policy's
  `inbound` for the class. - WS012 says "wall widened by user": your own `settings.json` allows writing or reading
  beyond the class's repos, or sets `permissions.disableBypassPermissionsMode` to something else. Take the setting out.
  - Keys Claude Code reads only from managed settings (`allowManagedHooksOnly`, `allowManagedPermissionRulesOnly`,
  `allowManagedDomainsOnly`, `allowManagedReadPathsOnly`) aren't in the launch file: in a `--settings` file they do
  nothing. The launch check and `strictAllowlist` do that work. See [ADR
  0027](../decisions/0027-a-settings-file-per-walled-class.md).

## Take it out again

ws never removes anything. `ws install --class NAME` prints these too (step 8), each line ready to paste into bash:

```sh
# the managed-settings file, and a copy of it a stopped paste left
sudo rm -f /etc/claude-code/managed-settings.d/50-ws-class-NAME.json /etc/claude-code/managed-settings.d/50-ws-class-NAME.json.tmp
# the launch file, a copy a stopped paste left, then the launch folder once no other class's file is in it
sudo rm -f /etc/claude-code/ws-classes/NAME.json /etc/claude-code/ws-classes/NAME.json.tmp
sudo rmdir /etc/claude-code/ws-classes
# the class account
# removes the account and its home folder
sudo userdel --remove ws-NAME
# your own home folder: put it back to the mode it had before step 2 (755 is the usual one)
chmod 755 "$HOME"
```

On macOS, `sudo sysadminctl -deleteUser ws-NAME` takes the account out.
