# TODO

## Phase 0 (repo setup)

- [x] Public repo, Apache-2.0 + NOTICE, squash-only, auto-delete head branches
- [x] Secret scanning + push protection, Dependabot alerts and security updates, immutable releases
- [ ] SSH signing key on GitHub; first commit Verified
- [x] Rulesets: `main` (no force push, no delete, linear, signed) and `v*` tags (no update/delete); no bypass; `ci` required
- [ ] CLA bot (CLA Assistant, hosted) linked to CLA.md
- [ ] `ws-toolkit` reserved on PyPI and npm

## Phase 1 (hygiene gate)

- [x] prek + gitleaks + PII regex + private denylist secret; `ci.yml` as the required check (planted key, IP and name each
  failed CI: PRs #2-#4, closed)
- [ ] ShellCheck and taplo hooks when the first shell or TOML file lands; licence check with the first dependency
