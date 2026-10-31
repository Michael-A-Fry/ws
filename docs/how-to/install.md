# Install ws

Install the `ws` command on macOS, Linux or Windows (through WSL2), and check that it runs.

## Before you start

- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/). The installer uses whichever it finds, uv first.
- On Windows, WSL2 with a Linux distribution. Run every command below inside it. Native Windows isn't supported.

## Steps

1. Run the installer.

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash
   ```

   With no options, the installer takes the wheel of the latest GitHub release, checks its `.sha256` file (and its cosign
   bundle if `cosign` is on your `PATH`), and refuses to install if a check fails. While no release exists, it installs
   the `main` branch and says so.

2. Check that `ws` runs.

   ```sh
   ws --version
   ws doctor --offline
   ```

   `ws --version` prints one line. `ws doctor --offline` prints one line per check. Exit code 1 and several
   `unconfigured` lines are expected on a new machine: nothing is set up yet.

3. Set ws up.

   ```sh
   ws setup
   ```

   See the [first-run tutorial](../tutorials/first-run.md).

## Choose a different source

| You want | Run |
|---|---|
| A specific release | `bash install.sh --version vX.Y.Z` |
| The package from PyPI | `bash install.sh --pypi` |
| A checkout on this machine | `bash install.sh --from PATH` |
| To see what would happen | `bash install.sh --dry-run` |

Save the script first if you want to read it before you run it: `curl -fsSLO <the URL above>`, then `bash install.sh`.

To install by hand, use `uv tool install ws-aitoolkit` or `pipx install ws-aitoolkit`.

## Success

`ws --version` prints a version, and `ws doctor --offline` finishes and prints a line for each of WS001 to WS007 and
WS101 to WS110. WS001 and WS002 are `ok`.

## If it fails

- `ws: command not found`: the folder that holds the command isn't on your `PATH`. Run `uv tool update-shell` or
  `pipx ensurepath`, then open a new terminal.
- The installer says neither uv nor pipx is installed: it prints the exact line for your system.
- WS001 fails on Windows: you aren't in WSL2. See [Troubleshoot](troubleshoot.md).

To check a download before you run it, see [Verify a release](verify-a-release.md).
