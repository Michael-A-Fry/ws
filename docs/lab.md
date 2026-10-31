# The lab VM cell

The lab cell is one more place the same timed run happens: install ws, set it up, run a first task against the mock
model, rate it, uninstall, check nothing is left behind (`scripts/e2e_first_task.sh`). The other cells (Linux, macOS,
Windows via WSL2) run on GitHub's hosted runners in `.github/workflows/cli.yml`. The lab cell runs on a virtual machine
in the lab (Proxmox), through `.github/workflows/lab.yml`, so a release is also timed on a machine that is not a
disposable cloud image. It is Tier 1 of the test plan (ADR 0011).

## What protects the runner

Leaving `pull_request` out of `lab.yml` protects nothing by itself. On a public repository a fork's pull request can add
its own workflow file with `runs-on: [self-hosted, ws-lab]`, and that file is the one that runs. A self-hosted runner
runs whatever job it is handed, so the protection is a stack, and each layer is there because the one above can fail:

1. **Outside contributors' workflow runs need approval.** The repository requires a maintainer's approval before any
   workflow runs for a pull request from an outside collaborator, first-timer or not. This is a repository setting and
   it is not in git; the repository owner sets it once (`gh` needs an admin token):

   ```sh
   gh api -X PUT repos/Michael-A-Fry/ws/actions/permissions/fork-pr-contributor-approval \
     -f approval_policy=all_external_contributors
   ```

   Check it with `gh api repos/Michael-A-Fry/ws/actions/permissions/fork-pr-contributor-approval`.
2. **Only the owner can start `lab.yml` from the repository.** It triggers on a pushed `v*` tag and on
   `workflow_dispatch` only. The `v*` tag ruleset (in place since phase 0) limits who can create such a tag, and
   `workflow_dispatch` needs write access. The workflow's first step also refuses to go on unless the repository is
   `Michael-A-Fry/ws`, the actor is the repository owner, and the event is a release tag or a dispatch. That step is a
   last check in the job, not a wall: a fork's own workflow would simply not contain it.
3. **The runner holds one job and the VM is thrown away after it.** The runner is a single-use (just-in-time)
   registration, and the host resets the VM from a clean snapshot after every job. A job that is somehow run on it
   (an approved fork run, say) finds a machine with no secrets, no token, no ws and no history, and leaves nothing
   behind for the next job.
4. **The VM holds no repository credential.** Only the one-time runner configuration (below) goes in. There is no
   admin token, no deploy key and no provider key on it, and the job's token is read-only (`contents: read`).

The risk that is left: a maintainer approves a fork's workflow run (layer 1) without reading the workflow file. That
run gets one job on a disposable VM on the lab network, with outbound network access. So: read the workflow file of any
fork pull request before approving it, and keep the VM on a network segment that cannot reach anything else in the
lab. Layer 1 is the one setting that has to be on; check it before relying on the lab cell.

## How the VM is reset and registered

Each job starts from a clean machine, and the host, not the workflow and not the VM, does the work:
`scripts/lab-host-cycle.sh` on the Proxmox host. One cycle is:

1. Stop the VM and roll it back to its snapshot, then start it.
2. Wait for the QEMU guest agent to answer.
3. Ask GitHub for a just-in-time runner configuration (`POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig`,
   label `ws-lab`). A JIT runner is always single-use: it takes one job, then GitHub removes it.
4. Write that configuration into the VM through the guest agent, as `/etc/ws-lab/jitconfig`.
5. Wait until GitHub forgets the runner, which is when its one job has finished (or a timeout, when the runner is
   removed through the API), then stop the VM.

The repository-admin token that step 3 needs lives on the host only, in a file (mode 0600) whose path is given as
`--token-file`. The VM never sees it. Run a cycle when a `lab` run is queued, from a timer on the host or by hand:

```sh
scripts/lab-host-cycle.sh --token-file /path/to/token --vm-id VMID --node NODE --snapshot SNAPSHOT
```

The script has not been run against a real host yet (CI cannot reach one); try it once by hand and read its output
before putting it on a timer.

What the VM image needs, all in the snapshot taken before any registration:

- Linux with Python 3.11 or newer, git, curl, bubblewrap and socat (Claude Code's sandbox), the kernel setting that
  lets bubblewrap make a user namespace (`kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04), and
  outbound HTTPS to GitHub, PyPI and npm. The runner user has no sudo. No provider key is on the machine: the run uses
  the mock provider.
- The QEMU guest agent, and the GitHub Actions runner package unpacked in the runner user's folder (get it from the
  repository's Settings, Actions, Runners, "New self-hosted runner" page). It is not registered: no `config.sh`.
- A folder `/etc/ws-lab` owned by the runner user (mode 0700), and a service that starts the runner when the
  configuration arrives, for example `ws-lab-runner.path` watching `/etc/ws-lab/jitconfig` and
  `ws-lab-runner.service`:

  ```ini
  [Service]
  User=runner
  WorkingDirectory=/home/runner/actions-runner
  ExecStartPre=+/bin/chown runner /etc/ws-lab/jitconfig
  ExecStart=/bin/sh -c 'exec ./run.sh --jitconfig "$(cat /etc/ws-lab/jitconfig)"'
  ```

## Until a runner exists

Until a cycle has registered a runner with the label `ws-lab`, a `lab` run does not fail: the job waits in the queue
("Waiting for a runner") and GitHub cancels it after 24 hours. A release tag therefore does not turn red for want of a
lab. Cancel a queued run by hand if you do not want it hanging around.

## Running it by hand

```sh
gh workflow run lab.yml --ref main
```

Then start a cycle on the host. The result is a job summary line and an `e2e-timing-lab` artifact holding
`e2e-timing.json`: every step's seconds and exit code, the total, the run id and the run's own duration.

## What the run proves and does not prove

It runs the real `ws broker serve`, started the way a person's service would be, on the default socket, and the real
`ws run`, `ws setup`, `ws rate` and `ws uninstall`, against a mock provider that serves https on 127.0.0.1 with a
throw-away certificate. The broker is told to trust that certificate with `SSL_CERT_FILE`, so the broker's TLS
relay is exercised. It does not prove the real provider's certificate chain or hostnames, the secret wall (a separate
system user holding the store; the broker here runs as the person), or anything about a real model's answers.
