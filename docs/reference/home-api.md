# ws home data API

`ws home data` prints one read-only view of your sessions, the questions waiting for you, your eval arms, your plugins
and recent events, merged from every place ws can read them. Every home area, app and script reads that one view, so
they agree. The Python side is `ws.home.api`; the shape is the envelope `ws.home/1`, which only ever gains fields.

```sh
ws home data            # a short summary and the state of each source
ws home data --json     # the whole snapshot, inside the usual `ws.cli/1` envelope, under `data`
```

## The snapshot

| Key | Holds |
|---|---|
| `schema` | `ws.home/1` |
| `ts` | When the snapshot was taken: RFC 3339, UTC, with a `Z` |
| `host` | This machine's name |
| `lead` | The one session your `[home] lead` setting names, or `null` |
| `asks` | What's waiting for you, oldest first |
| `sessions` | Every session, by host, then waiting first, then most recently seen |
| `plugins` | Each plugin: `name`, `on`, `built`, `region_ok`, `asks` |
| `evals` | One entry per eval arm |
| `events` | Events from the last 24 hours, newest first |
| `sources` | One row per source: `name`, `ok`, `kind`, `why`, `n` |

In Python: `api.snapshot(env)`, and one read for each part: `lead()`, `asks()`, `sessions()`, `plugins()`, `evals()`
and `events(since)`. Each takes an `Env` (`api.Env.make()` builds one from the real environment). A test passes its own
`run`, `now` and `host`, so nothing is global.

## Records

A **session** has `id` (the runner's own), `source`, `runner`, `name`, `host`, `tmux`, `cwd`, `repo` (the last folder of
`cwd`), `state` (`working`, `waiting`, `idle`, `ended` or `unknown`), `waiting_for`, `started_at`, `last_seen`,
`tokens` (`input`, `output`, `cache_creation`, `cache_read`, or `null`), `cost_usd` (or `null`), `cost_basis`
(`reported`, `otel`, `not measured` or `hidden`), `lead`, `opaque`, `opens` and `rc_url`.

An **ask** has `id`, `source`, `kind` (`question`, `permission`, `approval`, `waiting`, `draft` or `plugin`),
`session_id`, `session_name`, `tmux`, `repo`, `title`, `options` (each `label` and `description`; the first is the
recommended one), `decision`, `created`, `opaque`, `answer_in` (`app` or `terminal`), `opens` and `rc_url`. An ask is
answered in the app when its source gave a link to it, and in a terminal otherwise. A pending approval is always
answered in a terminal.

An **eval arm** has `arm`, `job`, `trials`, `solved`, `pass_rate`, `pass_hat_k` (`k`, `value`), `time_per_solved`
(`p50_s`, `p90_s`), `tokens_per_solved`, `cost_per_solved_usd`, `trend` (a list of `batch`, `ts`, `n`, `pass_rate`),
`noise` (`points`, `wins_needed`) and `not_measured`, which says why for each figure ws couldn't work out. Nothing is
estimated.

An **event** has `id`, `ts`, `source`, `type`, `session_id`, `summary` and `opaque`. The summary is built from the type
and the repo label only.

`opens` is empty here. The openers in the home page fill it in.

## Answering

`api.answer(ask_id, choice, text, env)` answers one ask, with one of its options (`choice`) or in your own words
(`text`). The source that gave the ask does it, by running `[home.answer] command`; ws never writes the source's file.
Only a question that isn't opaque, from a source that can take an answer (`asks_jsonl`), is answerable
(`api.answerable`). A permission, an approval, a waiting session and every opaque ask are answered where they live, and
asking the API to answer one raises `WsError` with kind `refused`. The other kinds are `not_found`, `not_set` (no
`[home.answer] command`), `failed` (the program didn't run or stopped) and `timeout` (it took too long, so the answer may
have gone). The command must use `{choice}` or `{text}`, or the profile is refused. A typed answer needs `{text}` and an
option needs `{choice}`; the page only offers what the command can take. The home server answers an ask once: a repeat
gets a 409.

## Sources

Sources are modules in `src/ws/home/sources/`, found by scanning the folder like the role adapters. To add one, add a
module there. It defines `NAME`, `PROVIDES`, `ORDER`, `available(env)` and `read(kind, env)`; the contract is in the
folder's `__init__.py`.

| Source | Kind | Reads |
|---|---|---|
| `claude_code` | sessions, asks | `claude agents --json` and the per-process files in `<claude dir>/sessions/*.json`. A session with `waitingFor` is also an ask of kind `waiting` |
| `ws_plugins` | plugins | The plugin registry and the profile's `[modules]` |
| `ws_runs` | sessions | Finished headless jobs in `logs/runs.jsonl`, with their cost. Eval trials show under `evals` |
| `ws_sessions` | sessions | The records `ws session start` writes |
| `ws_approvals` | asks | The gate's pending approvals, as asks of kind `approval` |
| `asks_jsonl` | asks | A JSON-lines file of ask events, folded into the asks still open |
| `ws_eval` | evals | `ws eval report`'s figures per arm, over the last 90 days |
| `transcripts` | sessions | Tokens per session, from `message.usage` in the session's transcript |
| `otel_jsonl` | sessions | Cost per session from `api_request` lines, over the last 14 days |
| `events_dir` | events | A folder of daily CloudEvents files |
| `collie` | sessions, asks | A slot. Not built until the phone front end's read format is known |

A source is used whole or not at all. If it can't be read (a missing program, a timeout, text that isn't JSON, a file
that won't open, a bug), none of its records are used and it becomes one row in `sources[]`. `kind` says why:
`unavailable` (there's nothing to read here), `unreadable`, `bad_format`, `timeout` or `failed`; `why` is one plain
sentence with no file contents. A source module that won't import is a `failed` row too. `ok` rows have `kind` `ok` and
`n`, the number of records. A line or file that isn't a JSON object isn't a record and is skipped.

Three rules hold for every source:

- A file ending in `.key` is never opened. The sources open files through one helper that refuses it, and list folders
  with `*.json` or `*.jsonl` patterns only.
- A transcript is read for its `usage` fields and nothing else, and the transcript of an opaque session isn't opened.
- `claude` runs through a timeout, with no shell, through `env.run`. Tests give `Env` a fake.

## The opaque rule

Some records must be opened in their own app to be seen. The rule fails closed: a record is shown only when something
known clears it, and every doubt hides it. A record is **opaque** when any of these holds:

- its source says it's redacted (an ask line that doesn't say `redacted: false` counts as redacted);
- its folder, or its repo, is under one of `[home] opaque_paths`, or its repo label is the last folder name of one;
- the session it belongs to is opaque, or isn't in the snapshot (a session is opaque if any source that knows it says
  so, judged piece by piece before the merge, so one source's opaque folder can't be lost behind another's open one);
- ws can't tell where it belongs. Only a known place that isn't opaque clears a record: a session needs a folder, and an
  ask or event needs a session in the snapshot that's open, or a folder that the source saw itself (the gate's record of
  an approval, or a live session's folder). A folder named in the asks or events file doesn't count, nor does an empty,
  relative or unreadable path, nor a session id ws doesn't know.

An ask that names a tmux target and no session id is placed by the one live Claude Code session in that pane or window
(`ws:@1.%1` is a pane, `ws:@1` a window id, and a window number such as `ws:2` is looked up with `tmux list-windows`).
That session's folder decides: open, and the ask is shown; under `opaque_paths`, and it's opaque. When the ask and the
session both carry a name, the names must agree, because a window number can move after the ask is written. No match,
or more than one session in the window, leaves the ask opaque.

Folders are compared as written: runs of spaces in a name aren't collapsed, and a path is cut off, never tidied, at 4096
characters (a longer one counts as opaque). A name is compared the way the file system compares names, and on a
case-insensitive file system a folder that's the same one on disk under another spelling (`~/Code/fiu` for
`~/code/fiu`) is covered too. Names are compared in one Unicode form, so `café` typed two ways is one name. The check
walks up each path and looks at every folder on the way, so a stale network mount can make it wait.

The rule runs last, after merging, and an opaque record keeps only these fields:

| Record | Kept |
|---|---|
| Session | `id`, `source`, `repo`, `tmux`, `state`, `started_at`, and `opaque` set; `cost_basis` is `hidden` |
| Ask | `id`, `source`, `kind`, `repo`, `tmux`, `created`, and `opaque` set |
| Event | `id`, `source`, `type`, `ts`, and `opaque` set |

Every other field is back at its empty default: no name, folder, title, tokens or cost. An opaque record's `id` is kept
only when it has a machine shape (a UUID, `waiting:` and a UUID, an ask id like `a20261102T120000-k3x9`, or 8 to 64 hex
digits); any other id, such as a case number, is replaced by a 16-character sha256 prefix.
An opaque session is never the lead. With `[home] lead` empty, the lead is the one live session whose folder is
`~/code` and whose name starts with `Home` or `WS Lead`, if exactly one matches; otherwise there's no lead.

## Sessions from several sources

A session is one `(runner, id)`. The sources that name it are joined: the earlier source wins a field, `last_seen` is
the later of the two, the state is the first that isn't `unknown`, and `source` lists all of them joined by `+`. When
`claude agents --json` was read, a Claude Code session it doesn't list has `ended`.

## The `[home]` profile table

ws reads and checks these keys. An unknown key is an error that names it, and ws never writes them.

```toml
[home]
lead = "cwd:~/code"             # or "name:Home*": the session the home pins at the top
opaque_paths = ["~/code/private"]  # absolute folders (or ~/...); a relative one is an error

[home.sources]                  # where a source reads; the defaults are under your home folder
asks_jsonl = "~/.claude-usage/asks/asks.jsonl"
events_dir = "~/.claude-usage/events"
otel_dir = "~/.claude-usage/otel"
claude_dir = "~/.claude"        # $CLAUDE_CONFIG_DIR when set

[home.open]                     # the opener for each kind of record (used by the home page)
session = "terminal"
ask = "browser"
opaque = "terminal"

[home.start]                    # the program the home page's box runs to start a session; no shell
command = ["ws", "dispatch", "{workspace}", "{request}"]

[home.answer]                   # the program that answers a question from its row; no shell
command = ["ws-ask", "answer", "{id}", "{choice}"]

[home.workspaces]               # folder name -> data class, shown beside it; an `opaque_paths` folder is "terminal only"
investments = "financial"

[home.models]                   # runner -> the models the box offers for it (none: the runner's own default)
claude-code = ["opus", "sonnet"]

[home.deny]                     # a runner, or runner/model -> the data classes it can't take
opencode = ["financial"]
```

`[home.start] command` and `[home.answer] command` are lists of words, the first the program. `{name}` in a later word is
replaced by a value, as one argument: `[home.start]` has `{what}` (`session`, `chat` or `job`), `{workspace}` (the
folder's short name), `{folder}` (its full path), `{where}`, `{runner}`, `{model}` and `{request}` (and needs `{request}`);
`[home.answer]` has `{id}`, `{choice}` and `{text}` (and needs `{id}`). A value that would start a word with a dash is
refused. When a terminal-only folder shares the short name of the folder you start in, a template without `{folder}` is
refused, because the program could pick either folder.

`lead = "cwd:PATH"` matches a session whose folder is exactly `PATH`. `lead = "name:GLOB"` matches by name. If several
match, the most recently seen one that hasn't ended is the lead.
