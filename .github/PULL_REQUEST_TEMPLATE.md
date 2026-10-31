# Pull request

## What and why

<!-- What does this change, and what problem does it solve? Link the issue. -->

## Commands run

<!-- List each command with its exit code. For example:
     uv run --locked pytest -q            exit 0
     uvx prek run --all-files             exit 0
     python3 scripts/check_docs_integrity.py   exit 0 -->

## Known gaps

<!-- What did you not run or not cover, and why? Write "none" if there are none. -->

## Compatibility risk

Does this change any of these? Tick each one that it does and say how.

- [ ] JSON output (an envelope, a field, an exit code)
- [ ] `policy.toml` (a key, a default, what a value means)
- [ ] An adapter (a runner or a role)
- [ ] A doctor ID (never reuse or change one; add a new ID)

## Checklist

- [ ] Docs updated for any changed command, flag, JSON field or policy key
- [ ] Commits are signed, with one author each
- [ ] No secrets, private hosts, IPs, e-mail addresses or names
- [ ] I did not merge or close any issue or pull request from automation

<!-- Structure adapted from the claw-code pull request template (https://github.com/ultraworkers/claw-code),
     Copyright (c) 2026 UltraWorkers and Claw Code contributors, MIT License. -->
