---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# Deny the home folder in the Claude Code launch settings, and re-allow the project and what tools need

## Context and Problem Statement

The Claude Code sandbox hides the paths in `sandbox.filesystem.denyRead`, and ws filled that list with the `deny_read`
paths of the policy (`~/.ssh`, the ws secrets folder and the like). Everything else in the home folder stayed readable
to a shell command: other tools' sign-in files, shell history, a browser profile, other projects. A person who names the
paths that matter is relying on remembering them all. The question is how to make a shell command in a walled session unable
to read the home folder by default, without a wall of our own and without breaking the common tools.

## Considered Options

- Write the home into `denyRead` everywhere ws writes settings (the shared file, the block merged into a person's own
  file, the OpenCode wall).
- Write it in the settings that `ws gate launch --runner claude-code` makes for one project, and re-allow what that
  session needs with the runner's own `allowRead`.
- Leave `denyRead` as the list of `deny_read` paths and tell people to add more.

## Decision Outcome

Chosen option: the launch copy only, using the runner's documented keys (`sandbox.filesystem.denyRead` and
`allowRead`), with no parser or sandbox code of ours.

The docs page checked on 2026-11-12, through context7 (`/websites/code_claude`): the settings reference
(<https://code.claude.com/docs/en/settings-reference>, entries "sandbox.filesystem.allowRead" and "Sandbox path
prefixes") and the sandboxing page (<https://code.claude.com/docs/en/sandboxing>, "Configure sandboxing" and its overlap
table), and the changelog for the first version (`allowRead` is 2.1.77; absolute paths in the folder lists work from
2.1.78). What they say, and what we rely on: `allowRead` re-opens paths inside a `denyRead` region; the narrower path
wins; an exact `denyRead` entry stays blocked inside a broader `allowRead`; a path starting `/` is absolute, `~/` is the
home, and anything else (`.` and `./x` too) is relative to the project in project settings and to `~/.claude` in user
settings. A file passed with `--settings` isn't a project file, so a relative entry would name `~/.claude`: the launch
copy never writes one. `wallsettings.MIN_CLAUDE` gets a row for `sandbox.filesystem.allowRead` at 2.1.78, so WS016 warns
on an older Claude Code and the e2e test skips from the same table.

What the launch copy (`claude_code.for_project`, called by `sandbox.launch_claude`) carries:

- `denyRead` gains the home folder as a real path, and as spelled when that differs (a symlinked `/home`). It keeps every
  exact `deny_read` path of the policy and of the secrets store (`policy.launch_read_rules`), and a `~/x` entry is also
  written under the real home.
- `allowRead` is the absolute, real project folder; the git folder and the common git folder when they're outside it (a
  linked worktree, whose `.git` file points into a `worktrees/` folder of the common git folder), found with
  `git rev-parse` as the OpenCode wall finds them; the default list; and the policy's new `[gate] allow_read`. An entry
  that doesn't exist is left out, and so is one whose real path is the home, above it, or inside a denied path, so a
  link can't re-open what it names.
- The default list is the minimum common tools need: three parts of the runner's state folder (`~/.claude`, or
  `CLAUDE_CONFIG_DIR`), `shell-snapshots/` (which every sandboxed command sources), `skills/` and `plugins/` (the
  parts of
  it that the sandboxing page says commands need), and never the folder whole; `~/.cache/uv`, `~/.npm`, `~/.cargo`,
  `~/.rustup`, `~/go`, `~/.cache/go-build`, `~/.local/bin`, `~/.local/lib`, `~/.local/share/uv`, `~/.gitconfig` and
  `~/.config/git`, the XDG variables of the launching shell deciding where the cache, data and config ones are. The
  rest of `~/.local` stays hidden: `~/.local/share` holds other
  tools' sign-in files.
- The credential files inside those folders stay hidden, as exact `denyRead` entries: the runner's `.credentials.json`,
  `~/.cargo/credentials` and `credentials.toml`, and git's `credentials` file.
- Write access is as it was: `denyWrite` still holds `.git/hooks` and the rest, and nothing is added to `allowWrite`.
- With `[gate] sandbox = false` there is no block, so nothing is added (ADR 0024).

### Why launch-only

The shared `settings.json` and the block `ws install` merges into a person's `~/.claude/settings.json` can't name the
project, and in a user file `.` means `~/.claude`, so a home-wide deny there would hide the project from every session
the person starts, including ones outside the wall. `userconfig.expected_sandbox` is unchanged, and so is
`policy.sandbox_read_paths`: the OpenCode wall and the macOS profile hide exactly those paths, and a hidden home would end
an OpenCode session. The home appears only in the launch copy, which is made once at each start: a change to the policy
or the defaults takes effect at the next start.

### Why the user file keeps its list

A person's own settings file is theirs, merged with every scope Claude Code reads, and it applies to sessions ws didn't
start. It keeps the `deny_read` paths ws has always put in it; deny-home is for the session ws starts.

A project's own `.claude/settings.json` that sets `sandbox.filesystem.allowRead` is still refused by the preflight
(`claude_code._widens_sandbox`), and Claude Code leaves a repository's `allowRead` out while the read block is on, so a
repository can't re-open the home.

### Consequences

- Good, because a shell command in a launched session can read the project and the tools' folders, and not the rest of
  the home, with no list to remember.
- Good, because it's the runner's own behaviour: the exact-deny rule means neither the project nor `allow_read` can
  re-open `~/.ssh` or the ws secrets folder.
- Good, because `ws canary` has a `wall` check that fails if the launch copy loses the home, the project, an exact deny
  path, or gains an unsandboxed route (an excluded command, a Unix socket).
- Bad, because a tool kept elsewhere in the home (a Node version manager, a language toolchain) can't be read until the
  person adds it to `allow_read`.
- Bad, because a `--separate-git-dir` project's git folder isn't re-allowed (a `.git` file is a pointer a repository
  can forge, so only a `worktrees/` folder of its common git folder is followed); naming the folder in `allow_read`
  fixes it.
- Bad, because a folder a tool creates in the home after the start can't be read until the session restarts.
- Bad, because a script a person keeps in the runner's state folder outside `shell-snapshots/`, `skills/` and
  `plugins/` can't be read by a shell command; the settings, history, transcripts, IDE lock files, file history, debug
  logs and the sign-in file are hidden on purpose. The runner reads its own state outside the sandbox (sandboxing covers
  shell commands only, checked 2026-11-12 on the permissions page), so that can't break it. `allow_read` re-opens one.
- Bad, because a session started another way (an OpenCode session, or `claude` started without `ws gate launch`) hides
  only the `deny_read` paths.

### Confirmation

`tests/test_sandbox_deny_home.py` (the launch copy, the exact deny paths and credential files, no relative path, the user
block unchanged, the sandbox-off case, linked worktrees, a symlinked home, the switch on and off), `tests/test_policy.py`
(`allow_read` validated whole), `tests/test_canary.py` (the `wall` check), `tests/test_doctor_wall_keys.py` and
`tests/e2e/test_sandbox.py` (a real `claude -p` can't read a file planted in the home or the ws secrets folder, reads the
project, and reaches an allowed host through the runner's own proxy and no other).
