---
status: accepted
date: 2026-11-23
decision-makers: maintainer
---

# An upgrade can run itself from a service that proves the release

## Context and Problem Statement

`ws upgrade` is refused inside a session, and has to be: a gated session that installed ws would be the gate changing
itself. So every upgrade needed a person at a terminal, and a lead that wanted a new release on the machine had to ask
for hands. Running it from a service outside every session removes the hands but not the risk, so the service has to
prove each release itself, wait until nothing is running, and be able to go back by itself, including when the new
release changed the policy in a way the old one rejects (the 22 November lockout, C706).

## Considered Options

- Keep upgrades manual, and give the lead a script that automates the typing.
- Let a session run `ws upgrade` under a looser gate for the lead.
- A systemd user service that takes a request file, proves the release from a trust root already installed, and
  checks and rolls back by itself, with a rescue timer behind it.

## Decision Outcome

Chosen option: the service. Four units (`ws upgrade --print-units`, written by `--write` from a person's terminal):

- `ws-upgrade.path` watches `<state>/ws-upgrade/request.json`, which holds one line: a release tag or the word
  `rollback`. It's outside every protected folder on purpose. It's a wish, not trust: the service reads it as data,
  checks the shape, takes it (the file is removed) and doesn't accept a path, a URL, a hash or a command from it.
- `ws-upgrade.service` runs `ws upgrade --unattended`, which is refused unless it runs under systemd with no session
  sign. It refuses a tag at or below the installed version and a second upgrade within an hour (`rollback` goes to the
  release before the last promote in the history, never to a tag the request names), proves the release, waits up to
  30 minutes for no running job and no broker lease (it never stops a job), installs the installer it proved with no
  mirror or skip-the-checks variable in its environment, and runs the acceptance checks. Any failed check is the
  automatic rollback of ADR 0031.
- `ws-rescue.timer` runs `ws-rescue.service` (`ws upgrade --rescue`) every ten minutes: it probes the gate as a
  watchdog does and, after two failures in a row, goes back one release, once.

Proof. The trust root (the release workflow's cosign identity, its OIDC issuer, and an allowed-signers file for release
tags) is stamped into the wheel at build time and read from the installed wheel only (`ws.version.trust_root`): a newer
release can only be trusted through the one already installed, and the first release that carries one is installed by a
person. `ws.upgradeproof.verify_release` then needs: the tag signed by an allowed signer, `install.sh` and the wheel
matching their `.sha256`, each cosign bundle verifying as made by the workflow's identity for that tag, every check run
on the tag's commit finished and none failed (the public API, no token), and no vulnerability of high or critical
severity, or with no severity, in the tag's lock (`uv audit`).

Checks after the swap (`ws.upgradecheck`): the new ws answers the tag, `ws upgrade --hooks-only` exits 0, and the gate
hook written in the runner's settings, fed a Bash payload, lets `ls -la` run and stops a private-key read and a read in
the secrets folder; the deny rules in the settings, and the must-deny probes the hook stops, are no fewer than before the
swap. A session isn't started (a subscription run per upgrade would also blur "no session runs the upgrade");
`--session-check COMMAND` adds one of the person's own.

A way back restores the files the new release changed (C706): the swap first holds the policy, the install record and the
runner configs, and an automatic rollback puts them back before the old hooks step. A manual `--rollback` restores the
policy from the copy kept in `<state>/upgrade-backups/` while it's still as the upgrade left it.

What stops a session: the `upgrade-service` guard refuses `systemctl` start, stop, restart, enable, disable, edit, kill
and the like on `ws-upgrade*` or `ws-rescue*`, `systemd-run` naming them, and `ws upgrade --unattended` or `--rescue`. It's
a guard and not a policy entry because a guard ships in the binary (an existing install's policy file is never
rewritten); `DEFAULT_GUARDS` and the upgrade's policy migration put it in an existing policy once. The four unit files
and the tool folder the units run from are protected paths of the hook (`ws.userconfig._claude_protect`), so a session
write there is refused. The units' `ExecStart` is the `ws` command inside the installed tool's folder, never a path
in a checkout or `~/.local/bin`.

Every outcome is a row in `<state>/release-log.csv` (at, verb, from, to, duration_s, result, reason).

### Consequences

- Good: a lead can ask for a release by writing one line, and nothing runs it but a service that has checked it.
- Good: a release that locks the gate is rolled back inside the swap, and by the rescue timer later if the lockout only
  shows up afterwards.
- Bad: the first release with a trust root, and the units, are installed by hand once. A machine with no `cosign` or
  `uv` on the service's PATH refuses every upgrade, and says so.
- Bad: the acceptance probes are a short fixed list, not the gate corpus (the corpus lives in the source tree and isn't
  shipped), and the installer for a rollback is fetched by tag, not proved, because releases before this one have no signed
  installer.
- The offline attestation check of the release's provenance isn't done: the release publishes the cosign bundle, which
  proves the same workflow identity, and not the attestation file.

### Swap notes

cosign is called in one function (`upgradeproof._cosign`), `uv audit` in another (`_audit`), systemd in one module
(`upgradeunit`), and the entry rule in one (`upgrade.unattended_refusal`); each can be replaced alone.

### Confirmation

`tests/test_upgrade_unattended.py` runs the service against a fake release tree (a really signed tag, hashes, bundles
a stub cosign checks, fake check runs) in a tmp home with the real gate: a good release upgrades, a bad one, a dropped
deny rule and a failing session check roll back, a wrong hash, a changed installer, an unsigned or foreign-signed tag, a
red check, a high advisory, a downgrade and a second upgrade within an hour are refused with nothing installed, a planted
mirror variable never reaches the installer, a running job makes it wait, and the rescue timer goes back once.
`tests/test_guard_upgrade.py` feeds the installed hook the refused and the allowed commands.
`tests/test_upgrade.py` holds the C706 end-to-end test: a failed check after a release adds a guard to the policy rolls
back to a release that rejects that guard, and the policy comes back with it.
