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

## Phase 3 (core)

- [ ] Policy schema, generated gate (Claude Code + OpenCode), broker, `ws run` + `runs.jsonl`, Memory + Notes modules
- [ ] Denied command blocked on both runners; a run record complete; no secret readable by `ws-dev`
- [ ] WS110 usage: Admin API keys are for organisations only; `adapter = "none"` until Michael decides
