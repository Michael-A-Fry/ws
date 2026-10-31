# ws documentation

ws is a small command-line toolkit: your memory, skills and rules, in any AI. These pages follow four kinds of need.
Pick the one that matches yours.

| You want to | Go to |
|---|---|
| Learn by doing, start to finish | [Tutorials](#tutorials) |
| Do one thing with exact steps | [How-to guides](#how-to-guides) |
| Understand why a rule exists | [Explanation](#explanation) |
| Look up a command, key or ID | [Reference](#reference) |

For a task-by-task list of every command, see [Usage](usage.md).

## Tutorials

- [First run](tutorials/first-run.md): install ws, set up a machine and watch the gate block a dangerous command.

## How-to guides

Set up and keep ws:

- [Install ws](how-to/install.md): install the command on macOS, Linux or WSL2.
- [Verify a release](how-to/verify-a-release.md): check the checksum, provenance and signature of a release file.
- [Upgrade and uninstall ws](how-to/upgrade-and-uninstall.md): move to a newer release, or remove ws.
- [Troubleshoot](how-to/troubleshoot.md): find and fix a problem by its symptom.

Apply the safety rules:

- [Use the gate](how-to/use-the-gate.md): write a policy and prove a denied command is blocked.
- [Use the wall](how-to/use-the-wall.md): start a session in a sandbox.
- [Approve a command](how-to/approve-a-command.md): let a command run once, with your yes.
- [Store a secret](how-to/store-a-secret.md): keep a key where an agent can't read it.
- [Apply the Rule of Two](how-to/apply-the-rule-of-two.md): refuse a job that could leak private data.
- [Set a budget](how-to/set-a-budget.md): cap monthly spend.
- [Send drafts only](how-to/send-drafts-only.md): lower the risk of an agent sending. Not enforced in v0.1.

Measure:

- [Run an eval](how-to/run-an-eval.md): test whether a change to your setup helps.

## Explanation

- [The gate](explanation/gate.md): no denied command, read or tool, whichever AI tool runs.
- [The wall](explanation/wall.md): a session writes only inside its project.
- [Approvals](explanation/approvals.md): an exact call runs once, after your yes.
- [The secrets broker](explanation/broker.md): a session never sees a secret's value.
- [The Rule of Two](explanation/rule-of-two.md): never untrusted input, private data and a way out together.
- [The budget](explanation/budget.md): no run without a monthly cap.
- [Draft-only sending](explanation/draft-only-sending.md): what exists, and what doesn't, in v0.1.
- [Known limits](explanation/known-limits.md): what ws doesn't do, in plain words.
- [Write-up 1](explanation/write-up-1.md): what phases 0 to 5 taught.

## Reference

- [CLI](reference/cli.md): every command and flag, the exit codes and the JSON envelope.
- [Doctor checks](reference/doctor-checks.md): every `WS` ID with its meaning and fix.
- [Policy](reference/policy.md): every `policy.toml` key.
- [Eval](reference/eval.md): task files, graders and the ten metrics.
- [Lab](reference/lab.md): the lab virtual machine cell and how it's protected.
- [`ws version --json`](reference/version-json.md): the build and install fields.

## Project files

- [SECURITY.md](../SECURITY.md): report a security problem.
- [SUPPORT.md](../SUPPORT.md): get help.
- [CONTRIBUTING.md](../CONTRIBUTING.md): change ws.
- [CHANGELOG.md](../CHANGELOG.md): what changed in each release.
