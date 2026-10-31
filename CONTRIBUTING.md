# Contributing to ws

A good change is small, tested and explained. This page is what to read before you open a pull request.

## Before you start

- Sign the [CLA](CLA.md). A bot asks on your first pull request, and the `license/cla` check shows the result.
- Open an issue first for anything bigger than a fix, so we can agree on it before you spend the time.
- Report a vulnerability through [SECURITY.md](SECURITY.md), never a public issue.

## Set up

You need [uv](https://docs.astral.sh/uv/) and Git.

```sh
git clone https://github.com/Michael-A-Fry/ws
cd ws
uv sync
```

## Check your change

```sh
uv run --locked pytest -q            # the whole suite (tests/e2e skips without runners)
uvx prek run --all-files             # gitleaks, ruff, typos, rumdl, actionlint, shellcheck, PII gate, globals, code map
uv run --locked ws doctor --offline  # the installed tool checking itself; exit 1 for unconfigured roles is expected
python3 scripts/gen_code_map.py      # regenerate the code map in AGENTS.md after a module docstring changes
python3 scripts/check_docs_integrity.py  # policy files exist, links and anchors resolve, no banned strings
```

The hooks that `prek` runs are listed in `.pre-commit-config.yaml`. CI also runs Vale (the Microsoft package) over the
README, the docs and the policy files, for errors only; [`.vale.ini`](.vale.ini) sets it up.

## Pre-push hook

The repo ships `.github/hooks/pre-push`. It runs `prek`, then the suite, before a push leaves your machine. Turn it on
once:

```sh
git config core.hooksPath .github/hooks
```

If you already use your own `core.hooksPath`, copy the hook into it or call it from yours. To skip it for one push, set
`WS_SKIP_PREPUSH=1`. That appends a line to `.git/ws-prepush-skips.log`, so a skip is never silent. CI runs the same
checks, so skipping only moves the failure.

## Rules for a change

[AGENTS.md](AGENTS.md) ("Conventions" and "Anti-patterns") is the source of truth, and it applies to people as much as
agents. In short:

- Stdlib only in `src/ws`. A new dependency needs a written reason in the pull request and a pin in `uv.lock`.
- Doctor IDs are stable. Add a new one; never change what an old one means.
- Docs change with behaviour, in the same pull request: a command, a flag, a JSON field or a `policy.toml` key.
- Tests set `HOME` and `WS_HOME` to `tmp_path`, build planted values at run time, and never read the real home, the
  timezone or the network.
- No private data. The PII gate enforces it.
- Pin everything: Actions by commit SHA, tools at least 7 days old.

## Commits and pull requests

- Sign every commit (SSH or GPG). `main` needs verified commits and a linear history (see `TODO.md` and
  [write-up 1](docs/explanation/write-up-1.md)).
- One author per commit, and no co-author trailers.
- Write the subject in the imperative, in about 72 characters or fewer, and put the why in the body.
- Fill in the pull request template. List the commands you ran with their exit codes, and the gaps.
- Pull requests are squash-merged.

## Triage labels

The maintainer uses four labels:

| Label | Meaning |
|---|---|
| `needs-repro` | Add steps, `ws version --json` and `ws doctor --json` |
| `duplicate` | Linked to the original and closed |
| `out-of-scope` | Not what ws is for, or a limit in [Known limits](docs/explanation/known-limits.md) |
| `accepted` | A real problem or a good idea; a pull request is welcome |

## AI tools

Use any tool you like, but you must understand the change and be able to explain it. Bulk or machine-written changes
you can't explain are declined.

Automation (bots, scheduled jobs and agents) never merges or closes an issue or pull request. It works on a branch and
opens a pull request, and a person decides.
