# Usage

Every ws command, grouped by the task it does. Each section links the guide for that task. The
[CLI reference](reference/cli.md) lists every flag.

Every command accepts `--json` (one JSON envelope on standard output) and `-C PATH` (run as if started in `PATH`).

## Install and set up

| Task | Command | Guide |
|---|---|---|
| Install ws | `bash install.sh` | [Install ws](how-to/install.md) |
| Answer the three machine questions | `ws init` | [First run](tutorials/first-run.md) |
| Set up step by step, in the terminal or a browser page | `ws setup` or `ws setup --web` | [First run](tutorials/first-run.md) |
| Set up with no prompts | `ws setup --yes` or `ws install --yes` | [Install ws](how-to/install.md) |
| See what an install would change | `ws install --dry-run` | [Install ws](how-to/install.md) |
| Switch a plugin on or off | `ws plugin list`, `add`, `remove` | [Install ws](how-to/install.md) |
| Add a second machine | `ws device add` | [Install ws](how-to/install.md) |
| Remove what ws installed | `ws uninstall` | [Upgrade and uninstall](how-to/upgrade-and-uninstall.md) |

## Check and fix

| Task | Command | Guide |
|---|---|---|
| Check the machine and every role | `ws doctor` | [Troubleshoot](how-to/troubleshoot.md) |
| See every file ws reads | `ws config` | [Troubleshoot](how-to/troubleshoot.md) |
| See the rules, skills and hooks each runner loads | `ws status` | [Troubleshoot](how-to/troubleshoot.md) |
| Check that a runner update broke nothing | `ws canary` | [Upgrade and uninstall](how-to/upgrade-and-uninstall.md) |
| Check for repo copies of global skills | `ws globals check` | [Doctor checks](reference/doctor-checks.md) |
| Get help for a command | `ws help COMMAND` | [CLI reference](reference/cli.md) |

## Keep a session inside your rules

| Task | Command | Guide |
|---|---|---|
| Write the default policy | `ws gate init` | [Use the gate](how-to/use-the-gate.md) |
| Turn the policy into a runner's rules | `ws gate generate` | [Use the gate](how-to/use-the-gate.md) |
| Test a rule against a payload | `ws gate check` | [Use the gate](how-to/use-the-gate.md) |
| Start a runner inside the wall | `ws gate launch` | [Use the wall](how-to/use-the-wall.md) |
| Approve one pending command | `ws approve` | [Approve a command](how-to/approve-a-command.md) |
| Refuse a job that holds all three legs of the Rule of Two | `ws run JOB --dry-run` | [Apply the Rule of Two](how-to/apply-the-rule-of-two.md) |
| Lower the risk of sending | policy settings | [Send drafts only](how-to/send-drafts-only.md) |

## Keep secrets from the agent

| Task | Command | Guide |
|---|---|---|
| Store a key | `ws secret set` | [Store a secret](how-to/store-a-secret.md) |
| List names, hosts and state | `ws secret list` | [Store a secret](how-to/store-a-secret.md) |
| Remove a stored value | `ws secret rm` | [Store a secret](how-to/store-a-secret.md) |
| Move the store behind its own user | `ws secret wall` | [Store a secret](how-to/store-a-secret.md) |
| Run the broker, or ask it for a lease | `ws broker serve`, `request`, `status` | [Store a secret](how-to/store-a-secret.md) |

## Run jobs and watch the spend

| Task | Command | Guide |
|---|---|---|
| Run a job | `ws run JOB` | [Set a budget](how-to/set-a-budget.md) |
| See what a run would do | `ws run JOB --dry-run` | [Set a budget](how-to/set-a-budget.md) |
| List the last runs | `ws runs` | [Set a budget](how-to/set-a-budget.md) |
| Rate a run | `ws rate RUN up` or `down` | [Run an eval](how-to/run-an-eval.md) |
| Put a job on a timer | `ws schedule list`, `apply`, `remove` | [CLI reference](reference/cli.md) |
| Send a push or the daily digest | `ws notify test`, `send`, `status` | [CLI reference](reference/cli.md) |
| Back up memory, notes and profile, and prove a restore | `ws backup init`, `run`, `verify` | [CLI reference](reference/cli.md) |

## Measure a change

| Task | Command | Guide |
|---|---|---|
| Run tasks against a job | `ws eval run` | [Run an eval](how-to/run-an-eval.md) |
| Read the ten metrics | `ws eval report` | [Run an eval](how-to/run-an-eval.md) |
| Compare two setups | `ws eval compare` | [Run an eval](how-to/run-an-eval.md) |
| Propose tasks from your own runs | `ws eval harvest`, `approve`, `reject` | [Run an eval](how-to/run-an-eval.md) |

## Remember things

| Task | Command | Guide |
|---|---|---|
| Capture a note to your `_inbox` | `ws note` | [CLI reference](reference/cli.md) |
| Make a memory folder | `ws memory init` | [CLI reference](reference/cli.md) |
| Add or search memory | `ws memory add`, `search`, `reindex` | [CLI reference](reference/cli.md) |
| Let a runner search memory and add notes | `ws mcp serve` | [CLI reference](reference/cli.md) |
| List or install the skills ws ships | `ws skills list`, `add` | [CLI reference](reference/cli.md) |

## Machine-read commands

Some commands are called by a runner, not by you: `ws gate check` (the hook), `ws session start` and `ws session nudge`
(the stale-session note), and `ws mcp serve`. They keep their own output. See the [CLI reference](reference/cli.md).
