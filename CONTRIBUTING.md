# Contributing to ws

Thanks for helping. ws is a small toolkit, so the bar is: a change is small, tested and explained. Structure adapted from
the claw-code CONTRIBUTING.md (<https://github.com/ultraworkers/claw-code>), Copyright (c) 2026 UltraWorkers and Claw Code
contributors, MIT License.

## Before you start

- Sign the [CLA](CLA.md). A bot asks you to on your first pull request; the `license/cla` check shows the result.
- Open an issue first for anything larger than a fix, so we agree on the problem before you write code.
- Report vulnerabilities through [SECURITY.md](SECURITY.md), not a public issue.

## Set up

You need [uv](https://docs.astral.sh/uv/) and Git.

```sh
git clone https://github.com/Michael-A-Fry/ws
cd ws
uv sync
```

## Run the checks

```sh
uv run --locked pytest -q            # the whole suite (tests/e2e skips without runners)
uvx prek run --all-files             # gitleaks, ruff, typos, rumdl, actionlint, shellcheck, PII gate, globals, code map
uv run --locked ws doctor --offline  # the installed tool checking itself; exit 1 for unconfigured roles is expected
python3 scripts/gen_code_map.py      # regenerate the code map in AGENTS.md after changing a module docstring
python3 scripts/check_docs_integrity.py  # policy files exist, links and anchors resolve, no banned strings
```

CI also runs [Vale](https://vale.sh/) with the Microsoft style package over `README.md`, `docs/` and the policy files.
Only errors fail the build. See [`.vale.ini`](.vale.ini).

### Run the checks before every push

The repository ships a pre-push hook that runs `uvx prek run --all-files` and `uv run --locked pytest -q`. Turn it on
once:

```sh
git config core.hooksPath .github/hooks
```

If you already use a custom `core.hooksPath`, copy or chain `.github/hooks/pre-push` instead. To skip it for one push,
set `WS_SKIP_PREPUSH=1`. The hook then logs a line to `.git/ws-prepush-skips.log`, so a skip is never silent. CI runs the
same checks, so a skip only moves the failure.

## Rules for a change

- **Stdlib only** in `src/ws`. A new dependency needs a written reason in the pull request and a pin in `uv.lock`.
- **Doctor IDs are stable.** Never change what a released ID means. Add a new ID.
- **Docs change with behaviour.** A new or changed command, flag, JSON field or `policy.toml` key updates the page that
  describes it, in the same pull request.
- **Tests set `HOME` and `WS_HOME` to `tmp_path`.** Build planted values at run time. Never read the real home, the clock's
  timezone or the network.
- **No private data.** No secrets, private IPs, hosts, e-mail addresses or names in code, fixtures, commits or comments.
  The PII gate fails them.
- **Pin everything.** Actions by commit SHA, tools at a version at least 7 days old.
- Read [AGENTS.md](AGENTS.md) for the module map, conventions and anti-patterns. It applies to people as much as agents.

## Commits and pull requests

- Sign every commit (SSH or GPG). The `main` branch requires verified commits and a linear history.
- One author per commit. No co-author trailers.
- Write the subject in the imperative and keep it under about 72 characters. Explain why in the body.
- Fill in the pull request template. **List the commands you ran, with exit codes, and the gaps**: what you didn't run
  and why.
- Pull requests are squash-merged.

## Triage and automation

The maintainer sorts issues and pull requests into four classes:

| Class | Meaning |
|---|---|
| `needs-repro` | We can't reproduce it. Add the steps, `ws version --json` and `ws doctor --json`. |
| `duplicate` | The same as another issue or pull request. We link it and close it. |
| `out-of-scope` | It doesn't fit what ws is for, or it's a limit we list in [Known limits](docs/explanation/known-limits.md). |
| `accepted` | We agree it's a real problem or a good idea. A pull request is welcome. |

You can use any tool to write a change, but you're the author: you must understand it and be able to explain it.
We decline bulk or machine-written changes that the author can't explain.

Automation (bots, scheduled jobs, agents) never merges or closes an issue or pull request. It works on a branch and
opens a pull request, and a person decides.
