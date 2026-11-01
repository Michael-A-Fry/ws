# Principles

The rules ws follows, one table per layer, each with the check that enforces it. A rule that no tool can check says so.
This page is the single place to look up a rule; `tests/test_principles.py` reads it and fails when a row breaks the
format below.

## How a rule becomes real

A rule that lives only in prose gets forgotten. Every row sits on one rung of this ladder, and the closer to T0, the
less it depends on anyone remembering:

| Rung | Meaning | Where it lives |
|---|---|---|
| T0 | Must always hold. A tool decides. | A test, a lint, a hook or a CI step |
| T1 | A short guideline every session reads. | `AGENTS.md`, `CONTRIBUTING.md` |
| T2 | Applies in one situation. | A skill or a path-scoped rule |
| T3 | Needs judgement. | A reviewer or the maintainer's eye |

How to read a table:

- **ID** is stable once published. A rule that goes away leaves its ID unused.
- **Check** is the path of the test, lint, script or workflow that enforces the rule, or `[judge]` when a person or a
  review decides. A T0 row always names a path.
- **Status** is `enforced` when the check exists today (for a `[judge]` row, when the review or skill is in place) and
  `planned` when the check isn't built yet. The count of planned rows can only go down.

To add a rule, add a row with its check in the same pull request. `tests/test_principles.py` fails on a duplicate ID, a
missing rung, a check path that doesn't exist, a T0 row marked `[judge]`, and on more planned rows than the baseline.

## Brand and tokens

One source of truth for every colour, size and font, so a restyle is a change of values.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| B1 | Type each colour, size and font value once, in `src/ws/brand.py`; the terminal, the pages and the docs read it. | T0 | `tests/test_brand.py` | planned |
| B2 | Use two tiers: a palette, then role names (accent, done, warn, text, muted, background, panel, line). | T0 | `tests/test_brand.py` | planned |
| B3 | Give dark and light the same role keys; a mode changes values, never structure. | T0 | `tests/test_brand.py` | planned |
| B4 | Meet WCAG 2.2 contrast for every text and background pair in every mode: 4.5:1 for text, 3:1 for large text and controls. | T0 | `tests/test_brand.py` | planned |
| B5 | Keep raw colour literals (`#rrggbb`, `rgb(`, `38;5;`) inside `src/ws/brand.py` and what it generates. | T0 | `tests/test_brand.py` | planned |
| B6 | Show status as a glyph plus a word; colour only reinforces it. | T0 | `tests/test_brand.py` | planned |
| B7 | Treat motion as a token that drops to zero under `prefers-reduced-motion`, and end every animation. | T0 | `tests/test_brand.py` | planned |

## Terminal colour

Colour is a capability the terminal grants, not something ws assumes.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| TC1 | Print colour only on a terminal, and turn it off for `NO_COLOR`, `TERM=dumb` and a pipe. | T0 | `tests/test_setup_ui.py::test_colour_is_off_for_no_color_pipes_and_dumb_terminals` | enforced |
| TC2 | Accept `--no-color` on every command. | T0 | `tests/test_output_contract.py` | planned |
| TC3 | Pick the richest colour depth the terminal reports (truecolour, 256, 16, none) from the same tokens. | T0 | `tests/test_brand.py` | planned |
| TC4 | Carry status in the basic 16 colours, which people re-theme; use 256-colour and truecolour for the mark and accents. | T0 | `tests/test_brand.py` | planned |

## UI

Rules for any page ws serves, not a look. The look comes from a reference set the maintainer approves.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| U1 | Build one representative page, get it approved, then restyle the rest (the `taste-gate` skill). | T2 | [judge] | enforced |
| U2 | Keep steady states quiet and spend emphasis on exceptions. | T3 | [judge] | enforced |
| U3 | Draw type, spacing and radius from one small fixed scale shared by every stylesheet. | T0 | `tests/test_brand.py` | planned |
| U4 | Give every pointer target at least 24 by 24 CSS pixels, set with `min-height`. | T0 | `tests/test_setup_ui.py` | planned |
| U5 | Keep focus visible: replace any `outline: none` with a visible focus style. | T0 | `tests/test_setup_ui.py` | planned |

## UX and writing

One voice across every writer, and most writers are agents.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| W1 | Lead with the answer; put the numbers first. | T1 | [judge] | enforced |
| W2 | Write plain NZ English in docs and messages; Vale (Microsoft style) fails the build on errors. | T0 | `.vale.ini` | enforced |
| W3 | Say what a thing does in help strings, descriptions and messages. | T3 | [judge] | enforced |
| W4 | Give every error a kind, a message that says what happened, and a hint that says what to do next. | T0 | `tests/test_output_contract.py::test_a_typed_error_has_kind_message_hint_and_exit_code` | enforced |
| W5 | Branch on an error's type, never on its message text. | T0 | `tests/test_run.py::test_the_table_is_keyed_by_exception_class_not_message` | enforced |
| W6 | Keep private IPs, hosts, e-mail addresses and names out of docs and messages. | T0 | `scripts/pii_gate.py` | enforced |
| W7 | Make every docs link and anchor resolve. | T0 | `scripts/check_docs_integrity.py` | enforced |
| W8 | File each page as a tutorial, how-to, explanation or reference (Diátaxis). | T1 | [judge] | enforced |

## Front end

The setup page is server-rendered HTML with one inline stylesheet and a little script. No framework, no build step.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| F1 | Keep the page local: it escapes what it prints and loads nothing from an external URL. | T0 | `tests/test_setup_ui.py::test_the_page_is_local_escaped_and_has_no_external_urls` | enforced |
| F2 | Bind the server to loopback and require the token, the host and the origin on every request. | T0 | `tests/test_setup_ui.py::test_the_server_is_loopback_only_and_checks_token_host_and_origin` | enforced |
| F3 | Send a strict Content-Security-Policy with `default-src 'none'` and name each source the page uses. | T0 | `tests/test_setup_ui.py` | planned |
| F4 | Make each read and write work as a link or a form post; script only enhances. | T0 | `tests/test_setup_ui.py` | planned |
| F5 | Use system fonts only. | T0 | `tests/test_setup_ui.py` | planned |
| F6 | Use native elements before ARIA; give each control an accessible name, and each page a `lang`, a title and a zoomable viewport. | T0 | `tests/test_setup_ui.py` | planned |

## CLI

`tests/test_output_contract.py` walks every command of the real parser, so a new command can't skip these rules.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| C1 | Give every command `--json`, answering with one envelope in the shape `ws.cli/1`. | T0 | `tests/test_output_contract.py::test_json_gives_one_envelope` | enforced |
| C2 | Print only the envelope on stdout under `--json`; send messages and errors to stderr. | T0 | `tests/test_output_contract.py::test_stdout_is_only_the_envelope_even_if_the_command_prints` | enforced |
| C3 | Cover every command in the contract test, or list it as raw with a reason. | T0 | `tests/test_output_contract.py::test_every_command_is_covered_or_listed_raw` | enforced |
| C4 | Suggest the closest command or flag when one is mistyped. | T0 | `tests/test_output_contract.py::test_unknown_command_suggests_the_closest` | enforced |
| C5 | Return exit code 0 for fine, 1 for needs doing, 2 for usage and 3 for refused; the envelope's `exit_code` is the process's. | T0 | `tests/test_output_contract.py::test_a_typed_error_has_kind_message_hint_and_exit_code` | enforced |
| C6 | Honour every prompt's answer under `--json` and keep the terminal for it. | T0 | `tests/test_output_contract.py::test_every_command_that_can_prompt_keeps_the_terminal` | enforced |
| C7 | Change the envelope only by adding fields; a breaking change bumps `SCHEMA`. | T0 | `tests/test_output_contract.py` | planned |
| C8 | Use the same verb for the same action across nouns, with no near-synonyms. | T0 | `tests/test_output_contract.py` | planned |
| C9 | Describe every command and flag in its help text, and lead the help with an example. | T0 | `tests/test_output_contract.py` | planned |
| C10 | Keep doctor check IDs unique and stable; a new check gets a new ID. | T0 | `tests/test_cli.py::test_check_ids_are_unique_and_stable` | enforced |

## Data

Plain files that open without ws, with a version on every record.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| D1 | Store data in open formats: Markdown, TOML, JSON lines, CSV or SQLite. | T1 | [judge] | enforced |
| D2 | Describe a run record with one JSON Schema (2020-12), and carry its version in every line. | T0 | `tests/test_run.py::test_the_schema_rejects_a_missing_key_an_extra_key_and_a_bad_status` | enforced |
| D3 | Append to logs under a lock, cap their size, and rotate them, so no line is lost or cut in half. | T0 | `tests/test_jsonl.py` | enforced |
| D4 | Within one schema version, only add fields; a breaking change takes a new version name. | T0 | `tests/test_run.py` | planned |
| D5 | Load the policy and the secrets registry whole or refuse; an unknown key is an error that names it. | T0 | `tests/test_policy.py::test_bad_policy_raises_with_a_plain_message_naming_the_key` | enforced |
| D6 | Keep secret values in `0600` files inside a `0700` folder, and print them nowhere. | T0 | `tests/test_secrets.py` | enforced |
| D7 | Keep memory as Markdown in a git repository, with a search index that can be rebuilt from the files. | T0 | `tests/test_memory.py::test_a_rebuild_from_scratch_gives_the_same_results` | enforced |
| D8 | Record who ran what in each run: run id, job, runner, ws version and times. | T0 | `tests/test_run.py::test_a_run_leaves_one_complete_record` | enforced |
| D9 | Keep secrets and personal data out of prompts, fixtures, commits and comments. | T0 | `.pre-commit-config.yaml` | enforced |

## Engineering governance

How ws changes without breaking what it promised.

| ID | Rule | Rung | Check | Status |
|---|---|---|---|---|
| G1 | Use the standard library only in `src/ws`; a new dependency needs a written reason. | T0 | `tests/test_cli.py::test_package_has_no_runtime_dependencies` | enforced |
| G2 | Import POSIX-only modules and names inside functions so native Windows can still run the doctor. | T0 | `tests/test_windows_import.py` | enforced |
| G3 | Deny when the payload, the policy or a rule can't be read (the gate fails closed). | T0 | `tests/test_gate.py::test_check_fails_closed_on_a_broken_policy_and_on_a_crash` | enforced |
| G4 | Write the `prepare` dial for anyone's policy or profile, never `act`. | T0 | `tests/test_output_contract.py::test_setup_yes_and_install_never_write_dial_act` | enforced |
| G5 | Keep tests from writing into the real runner config on the machine. | T0 | `tests/conftest.py` | enforced |
| G6 | Pin Actions by commit SHA and tools at versions at least 7 days old. | T0 | `tests/test_workflows.py::test_every_action_is_pinned_by_commit` | enforced |
| G7 | Keep no repo copy of a global skill, agent, workflow or hook. | T0 | `tests/test_globals.py` | enforced |
| G8 | Build the code map in `AGENTS.md` from module docstrings, and fail when it's stale. | T0 | `scripts/gen_code_map.py` | enforced |
| G9 | Make the release tag equal the version in `pyproject.toml`. | T0 | `.github/workflows/release.yml` | enforced |
| G10 | Name a check for every rule on this page, and let the planned count only fall. | T0 | `tests/test_principles.py` | enforced |
| G11 | Sign every commit, keep one author per commit, and never push to `main` from automation. | T1 | [judge] | enforced |
