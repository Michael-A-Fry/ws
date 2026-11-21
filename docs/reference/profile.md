# Profile

`profile.toml` in [the config folder](stores.md#where-ws-keeps-its-files) holds your own answers: the machine, the
roles, the spend cap and which model each job runs on. `ws config` lists the keys it sets. ws rewrites the file when a
command changes it, and the rewrite drops comments, so the setters below copy it to `profile.toml.bak-<UTC stamp>`
first.

## Which model a job runs on

A job file can leave `model` out. ws then picks the model in this order and stops at the first one it finds:

1. `model` in the job file.
2. `[models].<purpose>`, where `<purpose>` is the job's `purpose` (the same name its spend counts against).
3. `[models].default`.

If none of the three gives a model, the job still shows in `ws job list`, `ws config` and `ws doctor`, but `ws run`
refuses it before anything starts, with
`no model for job NAME: set model in the job file, or run: ws config model ID`.

```toml
[models]
default = "claude-haiku-4-5"   # any job with no model and no purpose entry
review = "claude-sonnet-4-5"   # jobs with purpose = "review"
```

A model id goes to the runner unchanged: a Claude Code name or alias, a `provider/model` id for OpenCode, or a
local provider's id. The run's own checks then judge the model the job resolved to, as if the job named it: a provider
the job's data class doesn't allow is refused, and a model the runner can't price needs `allow_unpriced = true` in the
job file.

A job that names its own `model` doesn't read `[models]` at all, so setting a default never changes it. `ws config model`
shows which jobs that holds for.

### Example with one purpose on a local model

The profile:

```toml
[models]
default = "anthropic/claude-haiku-4-5"
drafts = "ollama/gemma3:12b"
```

The job, `jobs/draft-notes.toml`:

```toml
runner = "opencode"
purpose = "drafts"
allow_unpriced = true    # a local model reports no cost; it's counted at your fallback price
prompt = "Draft the weekly notes."
```

`drafts` also has to be a purpose in `policy.toml` (`[budget.purposes]`), as for any job that names one.

## Commands

| Command | What it does |
|---|---|
| `ws config model` | Each job, its purpose, its model and where the model came from: `job`, `purpose`, `default` or `none`. |
| `ws config model ID` | Sets `[models].default`. |
| `ws config model PURPOSE ID` | Sets `[models].PURPOSE`. |
| `ws config budget USD` | Sets `[roles.budget] monthly_cap_usd`, the monthly spend cap in US dollars. A positive number only. |

Each setter prints the one key it changes (`[models] default: (unset) -> "claude-haiku-4-5"`) and asks before writing.
`--yes` writes without asking; `--dry-run` prints the change and writes nothing. Setting a key to the value it already
has writes nothing. After a write the setter prints the command that puts the old value back.

An agent session never changes the spend cap (`[roles.budget] monthly_cap_usd`), `[models]` or `[roles.model]`. The
check sits where the profile is written, so it holds whichever command gets there: `ws config budget`,
`ws config model` with a value, `ws install --cap` or `--model`, `ws setup --cap` and the setup page. Each is refused
(exit 3) before anything is written; run it in your own terminal. A write that leaves those keys alone isn't affected,
and the bare `ws config model` listing stays open to sessions. A run refused for having no model has `stop_reason`
`no_model` in its run record.
