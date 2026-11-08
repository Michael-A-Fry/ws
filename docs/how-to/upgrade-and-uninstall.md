# Upgrade and uninstall ws

Move to a newer release, or remove ws and what it installed.

## Upgrade

1. Run `ws upgrade` in a terminal of your own.

   ```sh
   ws upgrade
   ```

   It runs the installer (the latest stable release, checked as `install.sh` checks it), then rewrites ws's hooks in
   your runner's config with the new ws. It is one step because the hooks run whichever ws is installed, and a new ws
   under old hooks is the state this avoids. `ws upgrade` can't run inside a session, gated or not: the gate refuses
   the command, and it refuses itself when it sees a session or no terminal.

   To pick a release, add `--version vX.Y.Z`. To follow another channel, add `--channel rc` or `--channel edge` (see
   [Release channels](install.md#release-channels)). `--installer FILE` runs a copy of `install.sh` you have checked
   (see [Verify a release](verify-a-release.md)) instead of downloading it, and `--dry-run` shows the hook changes
   and installs nothing.

   `ws upgrade` ends in one of three states, and says which:

   - **The installer failed.** You still have the old ws and the old hooks, unchanged.
   - **The installer worked and the hook rewrite failed.** You have the new ws and your old hooks, put back byte for
     byte. They still work: a hook an earlier ws wrote has no pin on ws's folders, so the new ws runs the whole check
     for it and prints a note that names `ws upgrade`. Fix what the message says, then run `ws upgrade --hooks-only`.
   - **Both worked.** The new ws and the new hooks.

   A release before 0.1.9 has no `ws upgrade`. Run the installer yourself (`curl -fsSL
   https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash`), then run `ws upgrade --hooks-only`.
   Sessions work in between, each call with the note, as long as `HOME` is your own home folder; with another `HOME`
   an unpinned hook is refused. `ws doctor` shows the same state as `WS013`.

2. Check the version and the machine.

   ```sh
   ws --version
   ws doctor
   ```

3. Bring the rest of what ws installed up to date. This rewrites the generated gate files under
   `~/.local/state/ws/gate/` from your policy, and reports `0 changes` when nothing differs.

   ```sh
   ws setup --yes
   ```

If your profile has `gate = "every-session"` under `[roles.runner]`, `ws upgrade` and this step bring ws's entries in
your runner's own config file (for Claude Code, `~/.claude/settings.json`) up to date, in place. A profile that named a
runner before this setting existed stays on `launch`, so an upgrade doesn't touch that file. To move to every-session,
run `ws install --gate every-session`. [Known limits](../explanation/known-limits.md#the-gate-in-every-session) says
what goes into the file and what stays outside the gate.

An upgrade doesn't switch on anything you didn't have. If ws's entries in your config were written without the
sandbox block, the block stays out, whether `ws upgrade` or `ws install` rewrites them, until your policy says
`sandbox = true` under `[gate]` (then run `ws install`). It also keeps current the entries of every runner whose config
already holds them, not only the one your profile names. `ws upgrade` also never takes ws out of a config:
`ws install --gate launch` does that.

Once ws's entries are in your runner's config, a later `ws install` or setup run keeps them there and says so, even when
that run doesn't name a runner. The install manifest keeps every record, and every key from a newer ws, that a run
didn't write itself. Only the `launch` gate setting or an uninstall takes the entries out.

A session that was already running keeps the rules it loaded at start. When a newer release is installed, the session
gets one line about it, once. Start a new session to use the new release.

### Runner updates

ws never updates your AI tool. When `ws doctor` sees that a runner or the default model changed, it runs `ws canary` and
says so in WS101. A canary makes no paid model call. Run it by hand any time with `ws canary`.

## Move your files to the new folders

Releases before 0.1.8 kept everything in `~/.ws`. Now ws follows the XDG rules: what you write goes in
`~/.config/ws`, what ws writes as it works in `~/.local/state/ws`, the secrets store and eval tasks in
`~/.local/share/ws`, and the memory index in `~/.cache/ws`. Nothing moves by itself. While `~/.ws` is there, ws keeps
using it, and `ws doctor` shows a `WS010` warning.

1. See what would move.

   ```sh
   ws migrate --dry-run
   ```

2. Move it.

   ```sh
   ws migrate
   ```

   ws copies each file with its mode kept, checks every copy by its hash, writes a list of what moved to
   `~/.local/state/ws/migrate-manifest.json`, renames `~/.ws` to `~/.ws.migrated-DATE` and refreshes the runner hooks,
   which name your policy by its path. It deletes nothing: remove `~/.ws.migrated-DATE` yourself when you've looked.
   If a target already exists and differs from its source, ws stops before changing anything and names it.

3. If you have scheduled jobs, point their timers at the new folders.

   ```sh
   ws schedule apply
   ```

4. Check.

   ```sh
   ws doctor
   ```

   `WS010` is `ok`. If you set `WS_HOME` yourself, there's nothing to move: ws uses the folder you named.

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
   recorded come out, your edits stay, and `ws uninstall` says `kept your edits`. If the install record in the state folder
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
sits in `~/.local/state/ws/backups/` (mode 0600), named `claude-code-user-config-<time>.json`. `ws doctor` reports the same
state as WS008.

`ws install` refuses to put the hook in your config when the Python that runs the hook sits in a project's `.venv`, under
`.claude/worktrees/` or in a temp folder. Install ws with `install.sh`, pipx or `uv tool install` first, then run
`ws install` from that copy.

## What's kept

- Your notes folder and your memory folder, and anything you wrote in them.
- The work folders of jobs with no `cwd`, under `$XDG_STATE_HOME/ws` (default `~/.local/state/ws`). ws removes the
  empty run folders there, then `work/` and `ws/` once they're empty. A run folder that holds a file stays, with the
  folders above it, because what a job wrote is yours.
- The backup in `~/.local/state/ws/backups/`, when you'd edited your config file after ws wrote to it. After an exact
  restore the backup is the same as the file, so ws removes it.
- The secret store, if you moved it to its own user with `ws secret wall`. Remove that system user and its folder by hand.
