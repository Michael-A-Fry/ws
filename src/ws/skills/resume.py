"""The `resume` skill: pick up where an earlier session stopped."""

NAME = "resume"

TEXT = r"""---
name: resume
description: Pick up work after a cut-off, a cleared session or a new one - reads the repo's state file (else its TODO or recent commits), says in two lines where things are at and what is next, then carries on. Use when asked to "resume", "pick up where we left off", "carry on", "continue", "where did you get to" or "where are we".
---
# Resume

Goal: get back to work in a few minutes, from what the repo itself recorded, with no memory of the earlier session.

## 1. Read the record, cheapest source first

Stop at the first source that answers "what was in flight, and what is next":

1. The state file: `STATE.md` in the folder the repo ignores for it, at the main worktree root (or the state file the
   repo's own instructions name).
2. The repo's register: `TODO.md`, or the nearest equivalent (a backlog, a roadmap, an issues list the repo points to).
3. The git record: `git status`, `git branch --show-current`, `git worktree list`, and `git log --oneline -15`. Read the
   messages that start with `wip:`, since they mark work saved mid-way.

Read the repo's instruction file (`AGENTS.md` or `CLAUDE.md`) once if you have not already. Read files a note points to
only when the next step needs them.

## 2. Check the record against the repo

A state file can be older than the work. Compare it with what is on disk:

- Run `git status` in each worktree the state file lists. Uncommitted changes mean work after the last note.
- Check each branch it names: `git log --oneline <base>..<branch>` shows commits made after the note.
- Check that the first "Next" step has not already been done (look for its commit, its file or its test).

Where the record and the repo differ, go with the repo and note the difference.

## 3. Say where it is, in two lines

- Line 1: where it is at, with numbers (rows done of rows total, branches open, tests passing).
- Line 2: what is next, as the single concrete step you are about to take.

Add a third line only for a question that changes the plan, with your recommendation first. Settle everything else
yourself and say the assumption in a few words.

## 4. Carry on

Start the first step straight away. Work in the branch and worktree the record names. When the record names an agent
or job that already finished, use its result as it stands.

Keep the record current as you go: tick finished rows in the register, and rewrite the state file when a step lands
(a `checkpoint` skill, if installed, does this in one pass).

## When there is no record

If no state file, register or recent commits exist, say so in one line and ask what the task is.
"""
