# Set up a class user

Give a data class its own operating-system account, so a session of that class runs as that account and can't read your
home folder or another class's. ws prints the commands and the file; you run them.

## Before you start

- Linux, WSL2 or macOS, and an account that can use `sudo`.
- A class in `policy.toml` that names the account and a repo folder it works in. See
  [`[classes.NAME]`](../reference/policy.md).

  ```toml
  [classes.work]
  access = "walled"
  os_user = "ws-work"
  repos = ["/srv/work/**"]
  ```

  A repo is a folder, or a folder with a trailing `/**`. A wildcard in a folder name (`/srv/work/ops-*`) is refused with
  a usage error, because the file and the `chown` would then cover the folder above the repos; list each repo instead.
  Use a folder the account can reach. A `~` in a repo means the account's home (`/home/NAME`, or `/Users/NAME` on macOS),
  whether or not the account exists yet; a folder under your own home is closed to the account by step 3.
- Claude Code with the sandbox (bubblewrap and socat on Linux). See [Use the wall](use-the-wall.md).

## Steps

1. Ask ws what to run. It reads the policy and the system's user list, writes nothing and creates no user.

   ```sh
   ws install --class work
   ```

   An unknown class, or a class with no `os_user`, is a usage error (exit 2) that says what to add.

2. Read what it printed. A managed-settings file applies to every account on this machine, yours included, not only to
   the class's account. The file requires the sandbox, turns off the unsandboxed retry and bypass mode, lets sessions
   write only in the class's repos and the scratch folder, hides the home folder from them except the repos, and records
   that only the lead may send in.

3. Run the commands it printed, in order: create the account and close its home folder, close your own home folder
   (`chmod 700 "$HOME"`), give the account the repos, then place the file. The file goes in a managed-settings folder
   (`/etc/claude-code/managed-settings.d` on Linux, the `ClaudeCode` folder under `/Library/Application Support` on macOS)
   as `50-ws-class-NAME.json`, owned by root, readable by everyone and writable by no one else. It is written as
   `50-ws-class-NAME.json.tmp` first and moved into place, so a paste that stops half way leaves nothing Claude Code
   would read.

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

6. Check it.

   ```sh
   ws doctor
   ```

## Success

`ws doctor` prints one WS014 line per part for the class, all `ok`: the OS user exists, the file is present and
root-owned, the sandbox is required, unsandboxed commands are off, bypass is off, writes are limited, the home folder is
hidden and inbound is lead-only. A part that isn't ok says which one, and the `fix:` line says what to run.

## If it refuses

- `ws gate launch` exits 3 for a session of the class started as any other account. The message names the class, not the
  account. ws reads the account from the system's user list, never from `$USER` or `$HOME`, and the class from where
  the session starts, so setting those changes nothing. Start it as the class's account.
- WS014 `fail` on the file line: the file is a link, isn't owned by root, someone else can write it (or its folder), or
  the account can't read it (a file nobody else can read means its sessions run without the wall). Run the `fix:` line;
  it is the command for the problem the line names.
- More than one class with an account on one machine: Claude Code merges every managed file, so each class's sessions are
  allowed to write the repos of all of them. Keep classes that must not share on separate machines.
- The inbound row says the file records lead-only. That setting is a record; what lets a session send in is the
  policy's `inbound` for the class.
- WS012 says "wall widened by user": your own `settings.json` allows writing or reading beyond the class's repos, or sets
  `permissions.disableBypassPermissionsMode` to something else. Take the setting out.

To take the file out again: `sudo rm /etc/claude-code/managed-settings.d/50-ws-class-NAME.json`. ws never removes it.
