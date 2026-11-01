"""The `ship` skill: finish a piece of work properly and open a pull request."""

NAME = "ship"

TEXT = r"""---
name: ship
description: Finish a piece of work properly - runs the repo's own checks, reviews the diff, updates the register and state file, commits in the repo's style, pushes a branch and opens a pull request. Use when asked to "ship it", "wrap up", "finish this", "commit and push" or "open a PR".
---
# Ship

Goal: leave the work finished, checked and reviewable. The repo decides what "checked" means; use its commands.

## 1. Find the repo's rules

Read the repo's instruction file (`AGENTS.md`, `CLAUDE.md` or `CONTRIBUTING.md`) for its check commands, commit style
and branch habit. Look at `git log --oneline -10` to see the commit style in practice. Use the commands and style you
find there.

## 2. Run the checks

Run the repo's own commands, in the order it lists them: tests for what you changed, then the lint and format check,
then the wider suite when the repo asks for one before a push. Read each exit code.

- A red check is fixed, then the same command is run again. After two honest attempts, stop and report the failing
  output as it printed.
- Say which commands ran and which did not. A check that was skipped or could not run is reported as not run.

## 3. Review the diff

Read `git diff` against the base branch as a reviewer would. Look for: leftover debug output, stray files,
secrets or personal data, changed behaviour that lacks a test, and docs that have drifted from the code. Fix what you find
and run the affected checks again.

## 4. Update the record

- Tick the finished rows in the register (`TODO.md` or the repo's equivalent) and add rows for work you found.
- Rewrite the state file (`STATE.md` in the folder the repo ignores for it, if the repo uses one): goal, done, in
  flight, next.
- Add a changelog line when the repo keeps a changelog.

## 5. Commit in the repo's style

Stage the files by name. Write the message the way recent commits read (their tense, length and prefix). Commit as the
repo's configured author, with only the trailers the repo already uses. Several unrelated changes make several commits.

## 6. Push a branch and open a PR

- On the default branch, create a branch first and name it for the work (`git switch -c <name>`).
- `git push -u origin <branch>`, then open a pull request with `gh pr create`: a title in the commit style, and a body
  with what changed, why, and the check commands with their results.
- The default branch changes when the pull request is merged by its reviewer.

## 7. Report

Finish with: the branch, the PR link, the checks that ran with their exit codes, and anything left open.
"""
