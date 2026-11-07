# Install ws

Install the `ws` command on macOS, Linux or Windows (through WSL2), and check that it runs.

## Before you start

- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/). The installer uses uv, and falls back to pipx when
  uv isn't there.
- On Windows, WSL2 with a Linux distribution. Run every command below inside it. Native Windows isn't supported.

## Steps

1. Run the installer.

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash
   ```

   With no options, the installer takes the wheel of the latest stable GitHub release, checks its `.sha256` file (and its
   cosign bundle if `cosign` is on your `PATH`), and refuses to install if a check fails. While no release exists, it
   stops and says how to install the `main` branch instead (`--channel edge`).

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

## Release channels

ws comes in three channels. Pick the one that suits how much risk you want. Run the installer again with a different
`--channel` to move between them.

| Channel | You get | Run |
|---|---|---|
| `stable` (default) | The latest plain release, `vX.Y.Z`. Checked against its checksum and signature. | `bash install.sh` |
| `rc` | The newest release, release candidates (`vX.Y.ZrcN`) included. Checked like a stable release. | `bash install.sh --channel rc` |
| `edge` | The `main` branch, built from its repository. The newest work, and it can break. Nothing to check, because it isn't a release. Needs `git`. | `bash install.sh --channel edge` |

`ws version` shows the channel your install came from (`channel` in `ws version --json`).

To install a channel with the tool itself, without the installer:

```sh
uv tool install ws-aitoolkit                                      # stable
uv tool install --prerelease allow ws-aitoolkit                   # rc: pip and uv skip pre-releases unless asked
uv tool install git+https://github.com/Michael-A-Fry/ws@main     # edge
```

With pip or pipx, the pre-release switch for `rc` is `--pre` (`pipx install --pip-args=--pre ws-aitoolkit`).

### How releases are withdrawn

A bad release is yanked on PyPI and never deleted. A yanked version stays in the index, so an install that pinned it
still resolves, and a fresh install skips it. A release tag is never moved or deleted: a fix is a new version. The
GitHub release for a yanked version stays up, and its notes say why it was yanked.

## Choose a different source

| You want | Run |
|---|---|
| A specific release or release candidate | `bash install.sh --version vX.Y.Z` (or `vX.Y.ZrcN`) |
| The package from PyPI | `bash install.sh --pypi` (add `--channel rc` for pre-releases) |
| A checkout on this machine | `bash install.sh --from PATH` |
| To see what would happen | `bash install.sh --dry-run` |
| To rename a `ws` you already have | `bash install.sh --move-existing-ws-to PATH` |

Save the script first if you want to read it before you run it: `curl -fsSLO <the URL above>`, then `bash install.sh`.

## If you already have a program called `ws`

The installer looks in its install folder (`uv tool dir --bin`, or `~/.local/bin` for pipx) before it changes anything.
If a different `ws` is there, it stops before the download, keeps it in place, and prints two commands:

```sh
mv ~/.local/bin/ws ~/.local/bin/ws-legacy      # step 1: move it aside, then run the installer again
ws config fallback ~/.local/bin/ws-legacy      # step 2: after the install, hand it the words ws has no command for
```

`bash install.sh --move-existing-ws-to PATH` does both: it downloads and checks ws first, renames the old program just
before the install command (a symlink is recreated with its full target) and sets it as the fallback once ws is
installed. If the install fails, the old program goes back to its place. After that `ws doctor` runs ws's own
command, and `ws dispatch x` or a bare `ws` reach the old program with the same arguments. A program named
`ws-<word>` on your `PATH` also runs as `ws <word>`; see [Your own commands](../reference/cli.md#your-own-commands).

To install by hand, use `uv tool install ws-aitoolkit` (or `pipx install ws-aitoolkit`).

## Success

`ws --version` prints a version, and `ws doctor --offline` finishes and prints a line for each of WS001 to WS007 and
WS101 to WS110. WS001 and WS002 are `ok`.

## If it fails

- `ws: command not found`: the folder that holds the command isn't on your `PATH`. Run `uv tool update-shell` or
  `pipx ensurepath`, then open a new terminal.
- The installer says neither uv nor pipx is installed: it prints the exact line for your system.
- WS001 fails on Windows: you aren't in WSL2. See [Troubleshoot](troubleshoot.md).

To check a download before you run it, see [Verify a release](verify-a-release.md).
