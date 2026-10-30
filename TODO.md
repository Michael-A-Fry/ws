# TODO

## Phase 0 (repo setup)
- [x] Public repo, Apache-2.0 + NOTICE, squash-only, auto-delete head branches
- [x] Secret scanning + push protection, Dependabot alerts and security updates, immutable releases
- [ ] SSH signing key on GitHub; first commit Verified
- [ ] Rulesets: `main` (no force push, no delete, linear, signed) and `v*` tags (no update/delete); no bypass
- [ ] CLA bot (CLA Assistant, hosted) linked to CLA.md
- [ ] `ws-toolkit` reserved on PyPI and npm

## Phase 1 (hygiene gate)
- [ ] prek + gitleaks + PII regex + private denylist secret; `ci.yml` as the required check
