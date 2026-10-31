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
- [ ] ShellCheck and taplo hooks when the first shell or TOML file lands; licence check with the first dependency

## Phase 2 (CLI skeleton, doctor)

- [x] CLI skeleton, `ws doctor` (check IDs, `--json`), OS detection, the 3 questions (`ws init`), Console usage API check
  (WS110; needs an Admin API key, which Anthropic gives organisations only)
- [ ] `doctor` exits non-zero listing every role unconfigured on ubuntu-24.04, windows-2025 (WSL) and macos-26
