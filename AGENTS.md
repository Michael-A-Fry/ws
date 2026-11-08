# AGENTS.md: ws

For any coding agent working in this repo. It holds what you can't learn by reading the code, the tests or the docs.
Humans: start with README.md.

## Overview

`ws` is a small command-line toolkit: your memory, skills and rules, across your AI coding tools. It turns one
vendor-neutral `policy.toml` into each runner's own config (adapters for Claude Code and OpenCode), answers the runners'
hooks, and runs headless jobs that leave one complete record each. The core is Python 3.11+ and standard library only.
Apache-2.0, signed commits, one author per commit.

## Where to look

| Task | Look in |
|---|---|
| What a module is for | [docs/reference/code-map.md](docs/reference/code-map.md), built from the module docstrings |
| Why a design choice was made | [docs/decisions/](docs/decisions/README.md), one record per decision |
| A CLI command | a top-level module sets `COMMAND = "name"` and exposes `register(sub)`; `src/ws/cli.py` finds it by scanning (the older built-ins are still called by name there) |
| A doctor check | `src/ws/doctor.py` (IDs are stable: WS0xx machine, WS1xx roles) |
| What a session may do | `src/ws/policy.py` (parse and validate), `src/ws/gate.py` (decide, per runner) |
| A new runner or role adapter | add a module to `src/ws/adapters/` or `src/ws/roles/`; the contract is in the folder's `__init__.py` docstring |
| A headless job | `src/ws/jobs.py` (the file), `src/ws/run.py` (the run), `src/ws/runrecord.py` (the record) |
| Secrets | `src/ws/secrets.py` (store, registry), `src/ws/broker.py` (the only process that holds values) |

## Conventions

- **ws is for everyone.** Every feature, guard, test, default and doc must be generic and useful to anyone working with
  AI tools, on any runner, provider or OS. What only one person needs belongs in that person's own private config,
  outside this repo. Ask first: would a stranger who just installed ws want this, and does it work for them?
- Register: GitHub Issues (github.com/Michael-A-Fry/ws), board 'ws roadmap'; `TODO.md` is the Now list. File a bug, gap
  or idea as an issue with `kind:`, `area:`, `P0`-`P3` and `size:` labels (or `triage:needed`). A vulnerability goes
  to a private advisory, never an issue.
- Stdlib only in `src/ws`. A new dependency needs a reason written in the PR and a pin in `uv.lock`.
- One module per concern, and its docstring is its contract: what it does, why, and what it promises. The code map is
  built from the first sentence, so write it for a reader skimming a table.
- Adapters and roles are found by scanning their folder. The directory is the registry; no list to edit.
- A command module sets `COMMAND = "name"` on a line of its own at the top level and exposes `register(sub)`. `cli.py`
  reads the source of the installed package's top-level modules (never the working folder), imports only those that
  match, and calls `register(sub)` in module-name order after the built-ins. A name that is taken is a typed error, so
  a scanned command never shadows a built-in. No list to edit.
- Doctor IDs never change meaning once released. Add a new ID rather than reuse one.
- The gate fails closed: a payload it cannot read, a policy it cannot load or a rule it cannot parse is a deny.
- Package data (templates, HTML, skill text, schemas) ships as Python strings, because `ws secret wall` copies only
  `.py` files.
- Exit codes: 0 fine, 1 needs doing, 2 usage, 3 refused (`ws run`).
- Tests set `HOME` and `WS_HOME` to `tmp_path`, build planted values at run time, and never assume a timezone or an OS
  beyond what they skip on. CI runs in UTC. `tests/e2e/` needs real runners and skips without them.
- Adding or renaming a CLI command: add it to `CASES` in `tests/test_output_contract.py`.
- Pin everything: Actions by commit SHA, tools at versions at least 7 days old, `uv.lock`.
- Plain NZ English in docs and messages. No private IPs, hosts, e-mail addresses or names in the repo (the PII gate
  fails them).
- A new rule goes on `docs/reference/principles.md` with its check. A new design decision gets a record in
  `docs/decisions/`.

## Anti-patterns

- Never classify an error by matching its message. Raise a typed error with a kind and a hint, and branch on that.
- Never partial-load the policy or the secrets registry. Either the whole file validates or the command refuses.
- Never scaffold the `act` dial. Anything that writes a policy or profile for a person writes `prepare`; pin it with a test.
- No top-level import of a POSIX-only module (`fcntl`, `termios`, `pwd`, `grp`, `pty`, `resource`), and no POSIX-only
  `os` name at import time (`os.O_DIRECTORY`, `os.O_NOFOLLOW`, `os.getuid()` in a constant). Import or use it inside
  the function, in a `try/except ImportError`, or as `getattr(os, "O_NOFOLLOW", 0)`, so the native Windows doctor can
  still run and point at WSL2. `tests/test_windows_import.py` fails on the first module that dies.
- No test that reads the real HOME, the real `WS_HOME`, the clock's timezone or the network.
- No repo copy of a global skill, agent, workflow or hook (`ws globals check` fails it; list a deliberate one in
  `.ws/overrides.toml` with the reason).
- Never put a secret, a person's data or a private host in a prompt, fixture, commit or comment.
- Never merge, close or push to `main` from automation. Work on a branch and open a PR.
- Never develop a security fix on a public branch or in a public PR. Build it in the security advisory's temporary
  private fork and merge it with the release that publishes the advisory. A public PR carries no test, fixture or
  wording that shows a case the released version gets wrong.

## Commands

```sh
uv run --locked pytest -q            # the whole suite (tests/e2e skips without runners)
uvx prek run --all-files             # gitleaks, ruff, typos, rumdl, actionlint, shellcheck, PII gate, globals, code map
uv run --locked ws doctor --offline  # the installed tool checking itself; exit 1 for unconfigured roles is expected
python3 scripts/gen_code_map.py      # regenerate docs/reference/code-map.md after changing a module docstring
python3 scripts/check_docs_integrity.py  # links, anchors, policy files
git config core.hooksPath .github/hooks  # once: prek and the suite before each push (WS_SKIP_PREPUSH=1 skips, logged)
```
