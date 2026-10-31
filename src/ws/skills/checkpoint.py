"""The `checkpoint` skill: save what a fresh session needs to carry on."""

NAME = "checkpoint"

TEXT = r"""---
name: checkpoint
description: Save everything needed to resume this work in a fresh session - pins uncommitted work in every worktree and writes a state file. Use before the context or usage runs low, before a long fan-out of agents, before clearing the session, and when asked to "checkpoint", "snapshot", "save progress" or "pause".
---
# Checkpoint

Goal: a session with no memory of this conversation can carry on from the state file alone. Be fast and cheap; this
often runs when usage is nearly spent.

1. **Pin unsaved work, non-destructively.** For each worktree (`git worktree list`), if `git status` shows changes,
   commit them to that worktree's own branch with a message starting `wip:`. Never stash, reset or clean. If you cannot
   commit (a detached head, a merge in progress), say so in the state file rather than forcing it.
2. **Write the state file** at the main worktree root (overwrite it; keep it to 80 lines). Name it `STATE.md` in a
   folder the repo ignores (add that folder to `.git/info/exclude`, not to the committed ignore file), unless the repo
   already has a place for it. Sections:
   - **Goal:** what we are doing and why, in 2-3 lines.
   - **Done:** finished items, with commit SHAs or branch names.
   - **In flight:** each unfinished worktree, branch or agent: the branch, the files it owns, what remains. Note which
     agents already finished so nobody re-runs them.
   - **Decisions and rulings:** choices made and why, so a fresh session does not re-argue them.
   - **Next:** ordered concrete steps, each with the command or file to start from.
   - **Gotchas:** flaky tests, environment quirks, commands that do not work.
   - **Checkpoint refs:** the SHAs of the wip commits from step 1.
3. **Sync the register** (TODO.md or the repo's equivalent) if it has one: tick done rows, add new ones.
4. **Commit finished work** that is safe to commit on the current branch, following the repo's branch habit.
5. **Reply in 3 lines:** where the state file is, how many worktrees were pinned, and how to resume (start a new
   session and ask it to read the state file).
"""
