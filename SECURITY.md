# Security policy

This page explains how to report a vulnerability in ws privately, what counts as one and what to expect after you do.

## Supported versions

ws is pre-1.0. Fixes go into the latest release and `main`. Older releases aren't patched, so
[upgrade](docs/how-to/upgrade-and-uninstall.md) first if you can.

## How to report

Don't open a public issue for a vulnerability. Use GitHub's private vulnerability reporting instead:

1. Open the [Security tab](https://github.com/Michael-A-Fry/ws/security) of the repository.
2. Choose **Report a vulnerability**. The [direct link](https://github.com/Michael-A-Fry/ws/security/advisories/new)
   opens the same form.
3. Fill it in with the details below and submit it.

### What to include

- The output of `ws version --json`.
- Your OS and shell, and whether you're on WSL2.
- The command, policy or job file involved, with real values removed.
- The impact, and a safe proof of concept if you have one.

### What not to include

- Real API keys or other secrets.
- Private prompts, or session transcripts that hold secrets.
- Exploit payloads that act on other people's systems.

## What's in scope

- The secrets store and broker: reading a value, or making the broker hand one to an agent.
- The gate: a call the policy denies that the generated config or hook allows.
- The sandbox "wall": an escape, or a report that says the wall held when it didn't.
- Approvals: one that's forged, reused, used after it expired, or used for a different call.
- The installer and release artefacts: installing something other than the signed release, or passing verification
  with a tampered file.
- The run record: altering or removing a record without it being detected.

## What's out of scope

- Bugs in a runner that ws only configures.
- What a model does within the access the policy gave it.
- Attacks that need root, or the owner's account, first.
- Anything already listed in [Known limits](docs/explanation/known-limits.md).
- Social engineering, and denial of service with no practical security impact.

## What to expect

ws has one maintainer, so these are intentions, not promises:

- An acknowledgement within 5 working days (New Zealand time).
- A first assessment within 14 days.
- The report stays private until a fix or a workaround ships. Then you're credited, if you want to be.

## Related

- [Known limits](docs/explanation/known-limits.md)
- [Verify a release](docs/how-to/verify-a-release.md)
