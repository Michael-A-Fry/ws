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

3. Remove the command itself.

   ```sh
   uv tool uninstall ws-aitoolkit
   ```

   Use `pipx uninstall ws-aitoolkit` if you installed with pipx.

## Success

After step 2, `ws uninstall` prints how many paths it removed and how many it left. Run it again and it reports 0
removed. The command in step 3 prints that it removed the tool.

## What's kept

- Your notes folder and your memory folder, and anything you wrote in them.
- A work folder that holds your files, under `$XDG_STATE_HOME/ws` (default `~/.local/state/ws`). ws removes only empty
  run folders there.
- The secret store, if you moved it to its own user with `ws secret wall`. Remove that system user and its folder by hand.
