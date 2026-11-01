# ws: Your memory, skills and rules, in any AI

ws turns one `policy.toml` into each AI tool's own config, answers the tools' hooks, and runs headless jobs that leave
one complete record each. Your memory, skills and rules live in plain files that you own, so you can change tool or
model without starting again.

ws is a small command-line toolkit. The core is Python 3.11 or newer and the standard library only. It runs on macOS and
Linux. On Windows it runs inside WSL2.

## Install

Run the installer. It checks the machine, installs the `ws` command with uv or pipx, and runs a smoke test.

```sh
curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash
```

Or install the package yourself:

```sh
pipx install ws-aitoolkit
```

For other ways to install, including a pinned version, see [Install ws](docs/how-to/install.md).

## First command

```sh
ws doctor
```

`ws doctor` prints one line per check, each with a stable ID. `WS0xx` checks cover the machine and `WS1xx` checks cover
the roles you set up (runner, model, budget, secrets, memory, notes, scheduler, notify, backup, usage).

| Status | Meaning |
|---|---|
| `ok` | The check passed. |
| `warn` | It works, but something weakens it. Read the `fix:` line. |
| `unconfigured` | You haven't set this up yet. This is normal on a new install. |
| `fail` | Something is wrong. Read the `fix:` line. |

Doctor exits with 1 while anything needs doing, so a new install exits with 1. Run `ws init` to answer the three setup
questions, or `ws setup` to go step by step. `ws doctor --json` gives the same report for scripts.

To follow a whole first session, see the [first-run tutorial](docs/tutorials/first-run.md).

## Skills

`ws install` puts six skills in your runner's skills folder: `align`, `checkpoint`, `taste-gate`, `dataviz`, `resume`
and `ship`. `ws skills` lists them per runner. A skill you already have under one of those names stays as it is.

To use ws's version of a name you already have, run `ws skills adopt NAME`. Yours moves to
`$WS_HOME/backups/skills/<runner>/` and ws installs its own. `ws skills restore NAME` takes ws's copy out and moves yours
back.

## Verify

Check which build you have:

```sh
ws version --json
```

The output has `version`, `git_sha`, `dirty`, `build_date`, `python`, `install_path` and `install_source`. Each release
carries a checksum, a build provenance attestation, a Sigstore bundle and an SBOM. To check them, see
[Verify a release](docs/how-to/verify-a-release.md).

## Troubleshooting

| Problem | Fix |
|---|---|
| Neither uv nor pipx is installed | Install one: `curl -LsSf https://astral.sh/uv/install.sh \| sh`. The installer prints the line for your OS. |
| Python is older than 3.11 | Use uv, which fetches a current Python for you. |
| Native Windows | ws runs inside WSL2. Run `wsl --install -d Ubuntu-24.04`, then run the installer in that shell. |
| `ws` isn't found after the install | Run `uv tool update-shell` or `pipx ensurepath`, then open a new terminal. |

For more, see [Troubleshoot ws](docs/how-to/troubleshoot.md).

## Documentation

Start with the [documentation map](docs/README.md). Pages are grouped as tutorials, how-to guides, explanation and
reference (the [Diátaxis](https://diataxis.fr/) split).

| Page | What it covers |
|---|---|
| [Usage](docs/usage.md) | Every command, one task each |
| [First run](docs/tutorials/first-run.md) | Install, check, set up and run a first task |
| [Install](docs/how-to/install.md) | Install options and pinning a version |
| [Verify a release](docs/how-to/verify-a-release.md) | Checksum, attestation, Sigstore bundle |
| [Upgrade and uninstall](docs/how-to/upgrade-and-uninstall.md) | Move to a new version, or remove ws |
| [Troubleshoot](docs/how-to/troubleshoot.md) | Install and doctor failures |
| [Use the gate](docs/how-to/use-the-gate.md) | Turn the policy into each runner's rules |
| [Use the wall](docs/how-to/use-the-wall.md) | Run a runner inside the sandbox |
| [Approve a command](docs/how-to/approve-a-command.md) | Give a refused call a single-use yes |
| [Store a secret](docs/how-to/store-a-secret.md) | Keep a key where agents can't read it |
| [Apply the Rule of Two](docs/how-to/apply-the-rule-of-two.md) | Limit what one session can combine |
| [Set a budget](docs/how-to/set-a-budget.md) | Cap spend per month |
| [Send drafts only](docs/how-to/send-drafts-only.md) | Let agents draft, never send |
| [Run an eval](docs/how-to/run-an-eval.md) | Compare two setups on your own tasks |
| [The gate](docs/explanation/gate.md) | How policy becomes enforcement |
| [The wall](docs/explanation/wall.md) | What the sandbox stops and what it doesn't |
| [Approvals](docs/explanation/approvals.md) | Why an approval is bound and single-use |
| [The broker](docs/explanation/broker.md) | Why agents never see a secret |
| [The Rule of Two](docs/explanation/rule-of-two.md) | The design rule behind the policy |
| [Budget](docs/explanation/budget.md) | How caps are counted |
| [Draft-only sending](docs/explanation/draft-only-sending.md) | Why ws never sends for you |
| [Known limits](docs/explanation/known-limits.md) | What ws doesn't protect against yet |
| [Write-up 1](docs/explanation/write-up-1.md) | The design and what we measured |
| [CLI reference](docs/reference/cli.md) | Every command and flag |
| [Doctor checks](docs/reference/doctor-checks.md) | Every check ID |
| [Policy reference](docs/reference/policy.md) | Every `policy.toml` key |
| [Eval reference](docs/reference/eval.md) | Task format, graders, metrics |
| [Lab reference](docs/reference/lab.md) | The lab machine and the launch matrix |
| [`ws version --json`](docs/reference/version-json.md) | The fields of the version report |
| [Principles](docs/reference/principles.md) | The rules ws follows and the check for each |

For contributors: [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [SUPPORT.md](SUPPORT.md),
[CHANGELOG.md](CHANGELOG.md) and [AGENTS.md](AGENTS.md).

## Status

ws is pre-1.0. Commands, JSON output and `policy.toml` keys can still change between releases, and the
[CHANGELOG](CHANGELOG.md) says when they do. Doctor IDs never change meaning once released. Read
[Known limits](docs/explanation/known-limits.md) before you rely on the wall or the gate.

## Licence

ws is [Apache-2.0](LICENSE). Contributions need the [CLA](CLA.md), signed through a bot on your first pull request.
