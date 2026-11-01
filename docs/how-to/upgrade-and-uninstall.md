# Upgrade and uninstall ws

Move to a newer release, or remove ws and what it installed.

## Upgrade

1. Run the installer again. It takes the latest release.

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash
   ```

   To pick a release, add `-s -- --version vX.Y.Z` after `bash`. To check the file first, see
   [Verify a release](verify-a-release.md).

2. Check the version and the machine.

   ```sh
   ws --version
   ws doctor
   ```

3. Bring what ws installed up to date. This rewrites the generated gate files under `$WS_HOME/gate/` from your policy, and
   reports `0 changes` when nothing differs.

   ```sh
   ws setup --yes
   ```

If your profile has `gate = "every-session"` under `[roles.runner]`, this step also brings ws's entries in your
runner's own config file (for Claude Code, `~/.claude/settings.json`) up to date, in place. A profile that named a runner
before this setting existed stays on `launch`, so an upgrade doesn't touch that file. To move to every-session, run
`ws install --gate every-session`. [Known limits](../explanation/known-limits.md#the-gate-in-every-session) says what goes into the
file and what stays outside the gate.

A session that was already running keeps the rules it loaded at start. When a newer release is installed, the session
gets one line about it, once. Start a new session to use the new release.

### Runner updates

ws never updates your AI tool. When `ws doctor` sees that a runner or the default model changed, it runs `ws canary` and
says so in WS101. A canary makes no paid model call. Run it by hand any time with `ws canary`.

## Uninstall

1. See what would be removed. Nothing changes.

   ```sh
   ws uninstall --dry-run
   ```

2. Remove it.

   ```sh
   ws uninstall --yes
   ```

   `ws uninstall` removes only what the install manifest says ws created. A file you changed since is left and listed.
   Folders are removed only when they're empty, so your notes and memory stay.

   If ws added its entries to your runner's config file, uninstall takes them out again. When nothing else has changed
   that file since ws last wrote it, you get your original bytes back, exactly (or the file goes, if ws made it). When
   you've edited it at any point since the first write, even between two `ws install` runs, only the entries ws
   recorded come out, your edits stay, and `ws uninstall` says `kept your edits`. If the install record in `$WS_HOME`
   is lost but ws's hooks are still in the file, the next `ws install` records them again, so an undo takes them out and
   keeps the rest of the file.

   To take ws out of that file and keep the rest of ws, run `ws install --gate launch`. It does the same undo. A gated
   session can't run either command, because they would take off the session's own gate. Run them in a terminal of your
   own.

3. Remove the command itself.

   ```sh
   uv tool uninstall ws-aitoolkit
   ```

   Use `pipx uninstall ws-aitoolkit` if you installed with pipx.

## Success

After step 2, `ws uninstall` prints how many paths it removed and how many it left. Run it again and it reports 0
removed. The command in step 3 prints that it removed the tool.

## When the gate can't start

Every gated call goes through a hook that starts ws. When ws can't start (a moved Python, a removed install), the hook
blocks the call and says how to get unstuck. In a terminal of your own, either run `ws install --gate launch`, or open
`~/.claude/settings.json` and delete the PreToolUse group whose command contains `gate check`. The message also names
where a saved copy of your file is. If ws can't run at all, you can copy that file over your config file instead. It
sits in `$WS_HOME/backups/` (mode 0600), named `claude-code-user-config-<time>.json`. `ws doctor` reports the same
state as WS008.

`ws install` refuses to put the hook in your config when the Python that runs the hook sits in a project's `.venv`, under
`.claude/worktrees/` or in a temp folder. Install ws with `install.sh`, pipx or `uv tool install` first, then run
`ws install` from that copy.

## What's kept

- Your notes folder and your memory folder, and anything you wrote in them.
- The work folders of jobs with no `cwd`, under `$XDG_STATE_HOME/ws` (default `~/.local/state/ws`). ws removes the
  empty run folders there, then `work/` and `ws/` once they're empty. A run folder that holds a file stays, with the
  folders above it, because what a job wrote is yours.
- The backup in `$WS_HOME/backups/`, when you'd edited your config file after ws wrote to it. After an exact restore the
  backup is the same as the file, so ws removes it.
- The secret store, if you moved it to its own user with `ws secret wall`. Remove that system user and its folder by hand.
