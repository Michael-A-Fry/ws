# The lab VM cell

The lab cell is one more place the same timed run happens: install ws, set it up, run a first task against the mock
model, rate it, uninstall, check nothing is left behind (`scripts/e2e_first_task.sh`). The other cells (Linux, macOS,
Windows via WSL2) run on GitHub's hosted runners in `.github/workflows/cli.yml`. The lab cell runs on a virtual machine
in the lab (Proxmox), through `.github/workflows/lab.yml`, so a release is also timed on a machine that is not a
disposable cloud image. It is Tier 1 of the test plan (ADR 0011).

## Why it never runs on pull requests

A self-hosted runner executes whatever the workflow tells it to, on a machine on the lab network. On a public repo a
pull request can come from a fork, and its workflow file is the fork's. If the lab job ran on `pull_request`, anyone
could run code on that machine. So `lab.yml` has two triggers only: a pushed tag `v*` (needs write access to make) and
`workflow_dispatch` (needs write access to start). `tests/test_workflows.py` fails if `pull_request` or
`pull_request_target` appears, or if the job stops asking for the `ws-lab` label. `permissions` is `contents: read`,
and the checkout does not keep the token.

## How the VM is reset

Each job starts from a clean machine. The host (not the workflow) restores the VM from its snapshot before every job:

1. The snapshot is taken once, after the VM has the tools below and the runner package unpacked, and before the runner
   is registered. It holds no registration, no token and no ws.
2. The runner is registered `--ephemeral`, so it takes one job and then exits. A boot service on the VM asks for a
   fresh registration token, registers the runner and starts it.
3. When the job ends, the host restores the snapshot, which discards everything the job did, and boots it again.

The workflow resets nothing itself and `scripts/e2e_first_task.sh` only cleans up after itself inside a throw-away
`HOME`. If the host's reset is skipped, the run still works, but it is no longer a fresh machine and the timing is not
comparable.

What the image needs: Linux with Python 3.11 or newer, git, curl, bubblewrap and socat (Claude Code's sandbox), the
kernel setting that lets bubblewrap make a user namespace (`kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu
24.04), and outbound HTTPS to GitHub, PyPI and npm. The runner user has no sudo. No provider key is on the machine: the
run uses the mock provider.

## Registering the runner

On the VM, as the unprivileged runner user. Get the runner package for Linux from the repository's Settings, Actions,
Runners, "New self-hosted runner" page, and unpack it in a folder, then:

```sh
TOKEN="$(gh api -X POST repos/Michael-A-Fry/ws/actions/runners/registration-token --jq .token)"
./config.sh --url https://github.com/Michael-A-Fry/ws --token "$TOKEN" --labels ws-lab --ephemeral --unattended
./run.sh
```

The token lasts an hour and is single use; do not put it in the snapshot. The label `ws-lab` is what the workflow's
`runs-on` asks for. To remove a runner that is no longer wanted, in the same folder:
`./config.sh remove --token "$(gh api -X POST repos/Michael-A-Fry/ws/actions/runners/remove-token --jq .token)"`.

## Until a runner exists

Until a runner with the label `ws-lab` is registered, a `lab` run does not fail: the job waits in the queue ("Waiting
for a runner") and GitHub cancels it after 24 hours. A release tag therefore does not turn red for want of a lab. Cancel
a queued run by hand if you do not want it hanging around.

## Running it by hand

```sh
gh workflow run lab.yml --ref main
```

The result is a job summary line and an `e2e-timing-lab` artifact holding `e2e-timing.json`: every step's seconds and
exit code, the total, the run id and the run's own duration.
