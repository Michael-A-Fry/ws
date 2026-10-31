# Pull request

## What and why

<!-- What changes, which problem it fixes, and a link to the issue (for example "Closes #123"). -->

## Commands run

<!-- Each command you ran, with its exit code. For example:
- `uv run --locked pytest -q`: exit 0
- `uvx prek run --all-files`: exit 0
- `python3 scripts/check_docs_integrity.py`: exit 0
-->

## Known gaps

<!-- What you didn't run or didn't cover, and why. Write "none" if there's nothing. -->

## Compatibility risk

<!-- Tick everything this change may touch. The rules are in AGENTS.md under "Conventions". -->

- [ ] JSON output (envelope, a field or an exit code)
- [ ] `policy.toml` (a key, its default or its meaning)
- [ ] An adapter (a runner or a role)
- [ ] A doctor ID (never reuse or change one; add a new one)

## Checklist

- [ ] Docs are updated for any changed command, flag, JSON field or policy key
- [ ] Commits are signed, with one author each
- [ ] No secrets, private hosts, IP addresses, e-mail addresses or names
- [ ] Automation didn't merge or close any issue or pull request
