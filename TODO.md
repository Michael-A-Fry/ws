# TODO

## Phase 0 (repo setup)

- [x] Public repo, Apache-2.0 + NOTICE, squash-only, auto-delete head branches
- [x] Secret scanning + push protection, Dependabot alerts and security updates, immutable releases
- [x] SSH signing key on GitHub; local commits Verified (checked on 8a0064b), squash merges signed by GitHub
- [x] Rulesets: `main` (no force push, no delete, linear, signed) and `v*` tags (no update/delete); no bypass; `ci` required
- [x] CLA bot (CLA Assistant, hosted) linked to CLA.md: the `license/cla` check appeared on PR #5 (not a required check)
- [x] `ws-aitoolkit` 0.0.0 on PyPI and npm (#175); `ws-toolkit` held on PyPI, never released

## Phase 1 (hygiene gate)

- [x] prek + gitleaks + PII regex + private denylist secret; `ci.yml` as the required check (planted key, IP and name each
  failed CI: PRs #2-#4, closed)
- [x] actionlint hook (a workflow YAML error reached CI once)
- [ ] ShellCheck and taplo hooks when the first shell file or a hand-edited TOML lands; licence check with the first
  runtime dependency (the core has none)

## Phase 2 (CLI skeleton, doctor)

- [x] CLI skeleton, `ws doctor` (check IDs, `--json`), OS detection, the 3 questions (`ws init`), Console usage API check
  (WS110; needs an Admin API key, which Anthropic gives organisations only)
- [x] `doctor` exits non-zero listing every role unconfigured on ubuntu-24.04, windows-2025 (WSL) and macos-26
  (`cli.yml` on 778cfdf; native Windows fails WS001 and points at WSL2)

## Phase 2b (globals stay global, #178)

- [x] Shadowed-globals check: `ws doctor` WS005 + a prek hook in `ci`; a repo copy of a global skill, agent, workflow
  or hook fails unless listed in `.ws/overrides.toml` with a reason; listed overrides are reported
- [x] Stale-session nudge: a session records its ws release; after a newer one it gets one line, once
- [x] Pass test: a planted shadow fails; a listed override passes and is reported; an old-release session is nudged once
  (tests/test_globals.py, tests/test_session.py; PR #10)

## Phase 3 (core)

- [x] Policy schema + generated gate for Claude Code and OpenCode (PR #9): native deny lists, a fail-closed hook and an
  OpenCode shim; self-protection of `$WS_HOME`; Rule of Two with named exceptions; known matcher limits in policy.py
- [x] Secrets store + broker (PR #8): per-run leases to a reverse proxy that injects at the allowed host; `ws secret wall`
  moves the store to its own user with a root-owned copy of ws
- [x] `ws run` + `runs.jsonl` (PR #11): scrubbed env, broker leases, budget cap, Rule of Two, one record per run
- [x] Memory + Notes modules: front matter, `ws note`, `ws memory` (git + FTS5 index), `ws mcp serve`; WS105/WS106
- [x] Pass tests in CI (`cli.yml`): `gate e2e` (denied command blocked on Claude Code and OpenCode, stub model),
  `run record e2e` (record complete against the schema, both runners), `secret wall` (no secret readable by `ws-dev`)
- [ ] Secrets wall on Michael's machine: at the phase 6 cut-over (`ws secret wall`, one sudo script, re-paste keys)
- [x] Trust on first use of the root-owned /opt/ws copy: closed at phase T (installs come from the signed tag)
- [ ] Sandbox as the real wall behind the command matcher (phase 4); bound single-use approvals (phase 4)
- [x] WS110 usage: off (Michael: "leave it off"); `adapter = "none"`, spend comes from `runs.jsonl`
- [x] CLA signed by Michael (`license/cla` passes on PR #6)

## Phase 4 (setup, adapters, public skills, the wall)

- [x] Installer engine: `ws install` / `ws uninstall` with a manifest; a second install reports 0 changes, uninstall
  returns HOME to its pre-install state (tests/test_install.py, 3-OS matrix; PR #12)
- [x] Plugins by checkbox (#163, #164, #180): `ws plugin list|add|remove`; Finance is NZ-only; Obsidian/Logseq folder
  detection; `ws device add` (PR #12, #14)
- [x] Public skills align, checkpoint, taste-gate, dataviz, scrubbed (#165); `doctor` records runner versions and runs
  `ws canary` on a change; `ws rate <run> up|down` beside runs.jsonl (#162) (PR #14)
- [x] Role adapters anthropic, opencode, markdown-git, systemd, launchd, ntfy, restic, sops-age (PR #15)
- [x] The wall (PR #13): bound single-use approvals, a write-allowlist sandbox behind the matcher (bwrap + seccomp on
  Linux, sandbox-exec on macOS, Claude Code's native sandbox), OpenCode nudge e2e; three Opus cold-review rounds
- [x] Setup (PR #17): inline terminal setup and a localhost web page in the approved look (#182), wired to `ws install`
- [x] From the outside-project assessment (#183, #184): repo AGENTS.md with a generated code map, install.sh, notice file
  (PR #18); one CLI contract with typed errors, hints, `ws config`, `ws status` (PR #19)
- [x] Pass test on main: `ws setup --yes` twice reports 0 changes; `ws uninstall --yes` leaves 0 paths behind
- [x] **Before the tag:** the wall's open class (known limits in policy.py): OpenCode cache binaries writable inside
  the wall; the project lock misses not-yet-existing or renamed `.git/hooks`, `.claude`, `.opencode` (PR #23)
- [ ] P3: under `WS_OUTPUT=json`, an argparse or `-C` error before dispatch on `gate check` prints the envelope to
  stdout and nothing to stderr (still exit 2, still denies; the runner shows no reason)
- [ ] P3: tests leak `WS_OUTPUT` and `WS_GATED_SESSION` from the environment; clear them in conftest
- [ ] Known limit: the notify cap counts ws's own sends only; count notify leases in the broker
- [ ] Known limit: sops-age is named but not read by the broker yet

## Phase 5 (lab + evals)

- [x] The wall's open class (PR #23): a load-path table e2e (Claude Code, OpenCode, git) with real-loader controls;
  private per-project XDG data/cache; git-aware project lock with parent binds and placeholders; per-project Claude Code
  settings from `ws gate launch --runner claude-code`; seccomp refuses ptrace/process_vm_*; WS007 warns at
  ptrace_scope 0; an end-of-run report of anything that runs later; native-Windows import test for every module
- [x] Mock provider package, `tests/scenarios.json`, bounded JSONL logs, green levels, one typed recovery, every
  self-spawn `python -I -m ws` (PR #21)
- [x] Eval engine: task format, graders in the wall with a nonce, 10 metrics, McNemar per task, harvest (PR #22)
- [x] Matrix: timed install -> first task -> return on ubuntu, macOS and WSL2; credential-free smoke on 4 cells;
  `lab.yml` with a JIT-registered ephemeral runner (PR #24); fork-PR runs need approval for all outside contributors
- [x] **Lab cell (pass test):** VM 470 ws-lab (firewalled, snapshot reset, JIT runner): lab.yml run 41235914165 green
  (57 s); the host cycle ran by hand from the ws VM, a timer is phase 6
- [ ] Known limit: a repo the session makes with `git init`, submodules added mid-session and `sub/.claude` config
  are reported after the run (logs/wall-report.jsonl), not blocked
- [ ] Known limit: `/proc/PID/mem` of the runner is readable inside the wall at ptrace_scope 0 for a runner without
  the non-dumpable shim (Claude Code), and always as root; WS007 warns
- [ ] Known limit: the macOS wall (sandbox-exec profile) and PT_DENY_ATTACH in the eval checker are untested in CI
- [ ] Known limit: a session that keeps the runs log lock held blocks every `ws run` (fails closed, names the lock);
  without the wall a program the session starts can write `logs/`
- [ ] Known limit: the e2e does not exercise `ws run`'s own broker auto-start, a real provider's certificate chain
  or a real model
- [ ] Known limit: harvest checks secret names and shapes, not values; the grader's wall has the runner wall's network
- [ ] P3: a planted `$WS_HOME/canary.jsonl.old` folder silently stops canary lines
- [ ] P3: `PYTHONPYCACHEPREFIX` is set in the OpenCode wall only, so `.pyc` files crowd the Claude Code report
- [ ] P3: a `.git/hooks` folder with many files is not counted against the report's scan budget
- [ ] Not built: the rubric (model-judged) grader; time to first output and attention minutes need run-record fields

## Phase T (v0.1 thin tag)

- [x] Release workflow on `v*` tags: sdist + wheel, `.sha256` per asset, CycloneDX SBOM, build provenance, cosign
  keyless bundles, one immutable release, PyPI by trusted publishing; tag must equal the pyproject version and the
  run's commit; Opus cold review, 8 findings fixed
- [x] `ws version [--json]` (commit, dirty, build date, install source) with a CI check against the checkout's HEAD
- [x] `install.sh` installs the latest release's wheel after its SHA-256 (and cosign 3+ when installed) verifies
- [x] Docs to #158: README reordered, Diátaxis tree (30 pages), a page and a how-to per safety rule, known limits,
  SECURITY/SUPPORT/CONTRIBUTING/CHANGELOG, Vale (Microsoft) and the docs-integrity check in `ci`, PR template,
  pre-push hook, issue forms; private vulnerability reporting on
- [x] Write-up 1 (`docs/explanation/write-up-1.md`)
- [x] Restore drill 2 against the restic adapter: pass (AI_Workspace docs/plan/restore-drill-2026-11-01-restic.md)
- [x] Exit-arm run: qwen3-coder through OpenCode, 5 of 8 on the starter set, about US$0.04
- [x] PyPI trusted publisher for `ws-aitoolkit`: release 0.1.1 published through it
- [x] Triage labels (needs-repro, duplicate, out-of-scope, accepted) as GitHub labels
- [x] Pass test: v0.1.1 installed twice per cell, each with e2e: CI runs 41245122690 (6/6), lab 41245897083
- [x] Tag `v0.1.1` (0.1.0 was published, then withdrawn and deleted on GitHub and PyPI: DECISIONS 190)

## Phase 6 (harvest, then cut-over on the maintainer's machine)

Decisions (align, 1 Nov; DECISIONS 192, 193): every cut-over step gets a watchdog with automatic rollback (guards,
phone alerts, app sessions and scheduled jobs must not stop for a day). Mail switches on in drafts mode, a setting.
Descriptions, docs and messages say what a thing does.

Carried from phase T:

- [x] `ws broker serve` resolves its socket like the clients do (#35)
- [x] `ws secret import NAME --from-env PATH`: one store format for both tools (#35)
- [x] `ws eval report` lists trials that errored before a run apart from the metrics (#36)
- [x] The orphan-kill test in `test_run.py` waits on a signal, not the clock (#36)
- [x] Lab: the runner restarts after the jitconfig write (#38); the host cycle runs every 15 minutes when a lab run
  is queued
- [ ] Exit arm weekly on the scheduler: phase 7, with the schedule grammar

Step 1, gate and skills (harvest gaps):

- [ ] Hook kinds: claim check on messages and at Stop, test-file protection, skill-gated files with an override,
  a cap on concurrent subagents, a staging rule (`git add -A`, `git add .`, `git commit -a`), a syntax check after
  each write, the saved-state note at session start, Notification and Stop alerts through the notify role
- [x] Guard framework, PostToolUse/Stop/Notification events, `ws gate event`, call recording (paths and hashes for
  writes), bulk-staging, syntax-check and session-context guards (#41)
- [x] A replay tool: recorded tool-call payloads through the gate hook (`ws gate replay`, #41)
- [x] Skills `resume` and `ship`; `ws skills adopt|restore` swaps a person's same-named skill (#39)
- [ ] Apply the gate to every Claude Code session (merge into the user settings, manifest, residue-free undo), next to
  the person's own hooks
- [x] External subcommands (`ws <x>` runs `ws-<x>` on PATH) and an optional fallback for unknown words;
  install.sh moves a different `ws` aside (#40)
- [x] `ws notify alert`: session alerts to the phone on their own daily cap (#42)

Moved to phase 7 (DECISIONS 195: phase 6 builds and cuts over gate and skills only; the old timers stay on):

Step 2, timers:

- [ ] Schedule grammar: daily at a time, monthly on a day, nth weekday of the month, every N minutes, once at a date
- [ ] Jobs that run a script (argv); unit names that can't clash with units the person already has
- [ ] `ws models probe`: a daily diff of the provider's model list
- [ ] Watchdog job: checks each switched piece, runs its recorded rollback once, records it in the run log, alerts,
  then escalates (one automatic recovery per known failure)

Step 3, mail:

- [ ] `mail-drafts` role: triage new mail and save reply drafts, every 10 minutes; OAuth tokens held by the broker

Cut-over (each step only when `ws doctor` is green for it):

- [ ] Secrets wall on the maintainer's machine (one sudo script, then the keys re-pasted)
- [ ] Gate and skills switched; old hooks disabled, kept 30 days; rollback re-enable tested once
- [ ] Timers switched; old units disabled, kept 30 days; rollback re-enable tested once
- [ ] Mail switched on
- [ ] Pass test: old units disabled 30 days with nothing broken; a rollback re-enable tested

Known gate limits (found in review of #41; the sandbox is the wall behind the matcher):

- [ ] `xargs git add`, `git config alias.x '!git add -A'`, and `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` files that
  define an alias pass the bulk-staging guard
- [ ] False deny: an apostrophe in nested double quotes inside `$(...)` inside double quotes reads as an unclosed quote
- [ ] OpenCode copies of the new guards: phase 7

Adopted ideas (neutral wording): one automatic recovery per known failure, recorded, then escalate; green levels
(targeted, package, full, merge-ready) on hand-backs and run records with a known-flakes list; the pinpoint format
(fix, verification, review correction) for bug rows here.
