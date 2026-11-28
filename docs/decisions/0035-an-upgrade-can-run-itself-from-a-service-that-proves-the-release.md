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
- A systemd user timer whose service finds the newest release itself, proves it from a trust root already installed,
  and checks and rolls back by itself.

## Decision Outcome

Chosen option: the timer and its service. Two units, written by `ws upgrade --auto on` from a person's terminal:

- `ws-upgrade.timer` fires daily at a random minute of the hour (`Persistent=true`, so a day missed while the machine
  was off runs ten minutes after boot). No request file and no path unit: nothing a session writes names what gets
  installed.
- `ws-upgrade.service` runs `ws upgrade --unattended`, which is refused unless it runs under systemd, inside this unit's
  cgroup, with no session sign. It asks GitHub for the newest release (`releases/latest`, one tag read as data). It does
  nothing when that's the installed version, and skips a tag that has rolled back here before. Otherwise it proves the
  release and waits up to 30 minutes for no running job and no broker lease (it never stops a job). It copies the
  installed tool folder into the swap's `upgrade-backups/` folder, then installs the installer it proved with no mirror
  or skip-the-checks variable in its environment, restarts the broker on the new code (C848) and runs the acceptance
  checks. Any failed step after the swap is the automatic rollback of ADR 0031, done offline: the copy goes back in
  place, never a fresh download, which the network fault that broke the check would break too. A refusal or a rollback
  exits non-zero, and `OnFailure=ws-alert@%n.service` turns that into the ntfy alert every ws job has. A machine that
  stays busy exits 0 and is tried on the next run.

The switch (DEC-131) is `auto-upgrade.on` in ws's config folder, a protected root like the policy beside it: only
`ws upgrade --auto on` writes it, and it also writes the units and enables the timer. Enabling the timer alone isn't
enough, because a session can reach the user manager in more ways than a guard can list (its own timer with `Unit=`,
`Also=`, `Wants=` or `OnFailure=` in its own unit, a `timers.target.d` drop-in, a `.service.wants` link, `preset-all`).
So the service checks its own start with systemd (`upgradeunit.self_check`): the switch is on; `TriggeredBy` is the
timer alone and the timer fired just before this run; nothing else pulls either unit in (`WantedBy` beyond
`timers.target`, `RequiredBy`, `UpheldBy`, `OnFailureOf` and the like); no drop-in; no reload pending; and the unit files
are the text ws writes. Any of it failing is a refusal and an alert. That's also why `daemon-reload` is no longer
refused: a reload can only make the service refuse.

The installer runs with `UV_NO_CONFIG=1`, `UV_PYTHON_DOWNLOADS=never` and `PIP_CONFIG_FILE=/dev/null`, so a
`uv.toml` or `pip.conf` a session wrote isn't read, and with the tag's own lock as constraints (`uv export --frozen`,
pinned versions). Hashes aren't checked there: uv 0.13 ignores hashes in a constraints file for `uv tool install`
(measured, 28 Nov), so a pinned version is trusted to be the file PyPI serves for it. ws has no runtime dependencies
today, so the pin list is empty and the proved wheel is all that's installed.

A way back copies the kept tool folder beside the installed one first, then swaps them with two renames; a kill
between them leaves both beside it, and the unit's `ExecStopPost=` (shell, no Python) puts one back. The release is
proved before the idle wait, and the wait ends early enough that the install, its checks and a way back still fit in
the unit's hour. Busy runs exit 0, but three in a row alert, so a job or a lock held for days is seen.

There is no rescue timer. The checks inside the swap already roll back, and a second automatic action would need its
own switch (DEC-131). A rollback after the swap has been kept is the person's, from his terminal (`ws upgrade
--rollback`).

Proof. The trust root (the release workflow's cosign identity, its OIDC issuer, and an allowed-signers file for release
tags) is stamped into the wheel at build time and read from the installed wheel only (`ws.version.trust_root`): a newer
release can only be trusted through the one already installed, and the first release that carries one is installed by a
person. `ws.upgradeproof.verify_release` then needs: the tag signed by an allowed signer, `install.sh` and the wheel
matching their `.sha256`, each cosign bundle verifying as made by the workflow's identity for that tag, every check run
on the tag's commit finished and none failed (the public API, no token), and no vulnerability of high or critical
severity, or with no severity, in the tag's lock (`uv audit`).

What a tag signature proves, and what it doesn't. The signing key is the project's own, held by the people who tag, so a
valid tag signature shows that someone with that key tagged a commit on the main branch, not that a person reviewed and
approved the release. The protection is the combination: the commit is on main, every check on it passed, and the
release's files were built by the release workflow itself (the cosign identity). A leaked tag key alone doesn't pass,
and neither does a compromised workflow alone.

Checks after the swap (`ws.upgradecheck`): the new ws answers the tag, `ws upgrade --hooks-only` exits 0, and the gate
hook written in the runner's settings, fed a Bash payload, lets `ls -la` run and stops a private-key read and a read in
the secrets folder; the deny rules in the settings, and the must-deny probes the hook stops, are no fewer than before the
swap. A session isn't started (a subscription run per upgrade would also blur "no session runs the upgrade");
`--session-check COMMAND` adds one of the person's own.

A way back restores the files the new release changed (C706): the swap first holds the policy, the install record and the
runner configs, and an automatic rollback puts them back before the old hooks step. A manual `--rollback` restores the
policy from the copy kept in `<state>/upgrade-backups/` while it's still as the upgrade left it.

What stops a session: the `upgrade-service` guard refuses `systemctl` start, stop, restart, enable, disable, edit, kill
and the like on `ws-upgrade*`, `systemd-run` naming them, and `ws upgrade --unattended`. It's
a guard and not a policy entry because a guard ships in the binary (an existing install's policy file is never
rewritten); `DEFAULT_GUARDS` and the upgrade's policy migration put it in an existing policy once. The two unit files,
their drop-ins, the `.wants`, `.requires` and `.upholds` folders an enable links into, and the tool folder the units run
from are protected paths of the hook (`ws.userconfig._claude_protect`), so a session write there is refused, and a
session can't switch the timer on with a link either. The service runs outside the gate, in its own cgroup, which is how
it may rewrite the hooks that sessions can't.

Threat model. A tag signature guards against a tampered GitHub asset, not against an agent with release rights
(DEC-088): an agent that can tag and publish a release can ship what this service installs. That's accepted and logged
for C839. A session that writes a new unit of its own with `Wants=ws-upgrade.service` and starts it would start one run
early. That run still installs only the newest proved release, so it's accepted too.

The units' `ExecStart` is the `ws` command inside the installed tool's folder, never a path
in a checkout or `~/.local/bin`.

Every run is a row in `<state>/release-log.csv` (at, verb, from, to, duration_s, result, reason): `current`, `ok`,
`rolled-back`, `not-restored`, `refused`, `busy` or `skipped`. That's the evidence that ws runs the newest release
within a day, or the last one if the newest failed.

Not done, with the reason: waiting for no live interactive session. On this machine about 40 sessions are open,
most idle for days, so the upgrade would never run. A session keeps the rules it loaded, and the hook binary is
replaced by one install step.

### Consequences

- Good: no person or agent runs an upgrade: once the timer is on, each new signed release is installed within a day.
- Good: a release that fails its checks is rolled back inside the swap with no network, alerted on, and not tried again.
- Bad: the first release with a trust root, and the units, are installed by hand once. A machine with no `cosign` or
  `uv` on the service's PATH refuses every upgrade, and says so.
- Bad: the acceptance probes are a short fixed list, not the gate corpus (the corpus lives in the source tree and isn't
  shipped), and the installer for a manual rollback is fetched by tag, not proved, because releases before this one
  have no signed installer.
- Bad: a lockout that shows up only after the checks pass is the person's to roll back (no rescue timer, by choice).
- The offline attestation check of the release's provenance isn't done: the release publishes the cosign bundle, which
  proves the same workflow identity, and not the attestation file.

### Swap notes

cosign is called in one function (`upgradeproof._cosign`), `uv audit` in another (`_audit`), systemd in one module
(`upgradeunit`), and the entry rule in one (`upgrade.unattended_refusal`); each can be replaced alone.

### Confirmation

`tests/test_upgrade_unattended.py` runs the service against a fake release tree (a really signed tag, hashes, bundles
a stub cosign checks, fake check runs) in a tmp home with the real gate. A good release upgrades. A bad one, a dropped
deny rule and a failing session check roll back offline, including with the network cut after the install. A wrong hash,
a changed installer, an unsigned or foreign-signed tag, a red check and a high advisory are refused with nothing
installed. The installed or an older release changes nothing, a release that rolled back isn't tried again, a planted
mirror variable never reaches the installer, and a running job makes it wait. `tests/test_upgrade_lock.py` shows two runs
can't overlap. `tests/test_guard_upgrade.py` and `tests/test_guard_upgrade_live.py` feed the guard and the installed hook
the refused and the allowed commands, including the timer enabled by `systemctl` or by a link.
`tests/test_upgrade.py` holds the C706 end-to-end test: a failed check after a release adds a guard to the policy rolls
back to a release that rejects that guard, and the policy comes back with it.
