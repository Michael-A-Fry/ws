# Security policy

Structure adapted from the claw-code SECURITY.md (<https://github.com/ultraworkers/claw-code>), Copyright (c) 2026
UltraWorkers and Claw Code contributors, MIT License.

## Supported versions

ws is pre-1.0. Security fixes go into the latest release and the `main` branch. Older releases aren't patched: upgrade
to the latest. See [Upgrade and uninstall](docs/how-to/upgrade-and-uninstall.md).

## Report a vulnerability

Don't open a public issue for a suspected vulnerability. Use GitHub private vulnerability reporting:

1. Open the [Security tab](https://github.com/Michael-A-Fry/ws/security) of the repository.
2. Select **Report a vulnerability**.
3. Describe the issue, how to reproduce it and what it lets an attacker do.

Include:

- the output of `ws version --json`;
- your OS and shell, and whether you run in WSL2;
- the command, policy or job file involved, with real values removed;
- the impact, and any safe proof of concept.

Don't include real API keys, private prompts, session transcripts that hold secrets, or exploit payloads that change
other people's systems.

## Scope

In scope:

- the secrets store and the broker: a way to read a secret value, or to make the broker hand one to an agent;
- the gate: a call that the policy denies but that the generated config or the hook lets through;
- the sandbox (the wall): an escape, or a report that says the wall held when it didn't;
- approvals: forging one, or using one twice, after it expires or for a different call;
- the installer and release artefacts: a way to install something other than the signed release, or to pass a
  verification step with a tampered file;
- the run record: changing or removing a record without detection.

Out of scope:

- bugs in a runner (Claude Code, OpenCode or another tool) that ws only configures;
- what a model writes or does within the access the policy gave it;
- attacks that need root or the owner's account on the machine first;
- the limits listed in [Known limits](docs/explanation/known-limits.md);
- social engineering, and denial of service with no practical security impact.

## What to expect

These are intentions, not promises. ws has one maintainer.

- We aim to acknowledge a private report within 5 working days (New Zealand time).
- We aim to give a first assessment within 14 days.
- We keep the report private until a fix or a workaround is out, then credit you if you want that.

## Related

- [Known limits](docs/explanation/known-limits.md) lists what ws doesn't protect against yet.
- [Verify a release](docs/how-to/verify-a-release.md) shows how to check a download before you run it.
