# Inbox triage

`ws triage run` keeps an inbox sorted and leaves reply drafts in your Drafts folder before you read the mail. Most mail
is sorted by rules with no model at all; a model is called once for each thread that may need an answer, with no tools,
a small input and a strict answer shape; checks written in code refuse a draft that invents or promises something.
Nothing is sent, moved or deleted. The module is `ws.triage`; it reads and writes mail only through [`ws.mail`](mail.md).

## Why it's shaped like this

Mail is untrusted input, and a model that reads it can be steered by it. So the order is: what code can decide, code
decides; the model gets one narrow job; code checks what comes back. This is the "workflow with programmatic gates"
pattern from Anthropic's [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
(read November 22, 2026): use a fixed workflow where the steps are known, and an agent only where they aren't. A smaller
model costs less and needs more guard rails and checks that aren't a model, which is what the rules and checks are.

1. **Fetch, no model.** For each mailbox, the messages newer than the last uid seen. Nothing new is a run with no model
   call.
2. **Rules, no model.** A plain TOML file maps a sender, a domain, bulk-mail headers and subject patterns to one of
   `ws/needs-reply`, `ws/fyi`, `ws/waiting`, `ws/low` or to `ask-model`, which is a state, never a label. The first match
   wins and its id is logged with the decision. A rule only labels; it never drafts.
3. **Model, `ask-model` threads only.** One call per thread: no tools, one turn, the thread cut to the newest messages
   inside a token cap, plus your voice notes. It answers `{"needs_reply": bool, "reason": str, "draft": str | null}`
   and nothing else. A bad answer is retried twice, then the thread is flagged and nothing is drafted.
4. **Checks, no model.** A draft is refused, and the thread flagged with the reason, when it names an amount, a date (a
   weekday counts) or a time the thread doesn't carry, promises a payment, holds a link or address the thread doesn't
   carry, opens with "Dear" or isn't 2 to 6 sentences. A thread that asks for a time is drafted without one and flagged
   `scheduling: check your calendar`, because ws has no calendar source yet.
5. **Output.** The label and the draft go in through `ws mail`. Each run adds one line to `logs/triage.jsonl` and, when
   `summary_dir` is set, the day's `summary-DATE.md` is rewritten (it adds up across the day's runs).

A thread the model says needs no reply gets `ws/fyi`. A thread it says needs one gets `ws/needs-reply`, with a draft
when the checks pass.

## The job file

Put the settings in the job file, so one file holds the schedule and the settings. `command = "triage"` makes the timer
run `ws triage run --job NAME`; it needs the run contract lines of any scheduled job (the list is at the top of
`ws.jobs`).

```toml
command = "triage"
schedule = "every 30m 05:00-22:00"   # 05:00, then every 30 minutes, the last run at 22:00; see Schedules below
timeout_s = 600
on_failure = "alert"
log = "file"
uninstall = "ws schedule remove inbox"
expected_rate = "1d"
data_class = "none"

[triage]
mailboxes = ["personal"]             # names from [mail.NAME] in the profile
rules = "inbox-rules.toml"           # relative to the jobs folder, or absolute, or under ~
prompt_file = "inbox.md"             # the model's instructions: the schema and the task, short
voice = "~/notes/voice.md"           # optional: how you write replies (at most 3000 characters are used)
summary_dir = "~/notes/inbox"        # optional: summary-DATE.md goes here
first_run_days = 1                   # the first run reads this many days back; 0 = start from now, read nothing
max_drafts = 15                      # drafts in one run; the rest are flagged

[triage.mailbox.personal]            # optional, per mailbox
query = "-from:work.example.org"     # a search query (Gmail syntax for a gmail mailbox): mail it leaves out is never opened
class = "personal"                   # a data class from the policy; the run is refused unless it has full access

[triage.model]                       # the model is config
runner = "claude-code"
model = "haiku"
budget_usd = 0.10                    # the most one run spends on the model, all calls together
run_tokens = 400000                  # the most tokens (in and out, the runner's own context too) one run uses
thread_tokens = 1500                 # the most of a thread one call carries (counted by characters)
max_calls = 12                       # calls in one run, retries included
# bare = false                       # default: false for Claude Code with no key (its --bare ignores the subscription
                                     # login), true otherwise; the run's folder is empty, so no project context loads
```

`ws triage run --config FILE` takes the same settings from any TOML file (its `[triage]` table, or the whole file). Only
`--job` has a schedule.

A run that hits a cap holds the remaining threads for the next run (the state file keeps their uids) rather than losing
them. A runner that fails (not logged in, over its cap) stops the model calls for the run and holds the threads the same
way; the exit is 1 only when a mailbox failed.

## The rules file

```toml
default = "ask-model"                # what no rule matched: ask-model (default) or a label

[[rule]]
id = "bulk-headers"                  # lower-case letters, digits, - _ and .; logged with every decision
label = "ws/low"
bulk = true                          # List-Unsubscribe, List-Id, Precedence bulk/list/junk, or Auto-Submitted

[[rule]]
id = "bank"
label = "ws/fyi"
domain = "bank.example.nz"           # the domain and every sub-domain
# also: sender = "a@b.org" (or a list), subject = "^receipt\\b" (a case-insensitive pattern), mailbox = ["personal"]
```

Conditions in one rule are ANDed. A message from the mailbox's own address is left alone (`builtin:self`). Rules see
the headers only, never the body.

## What it costs

Every run writes its measured tokens and cost to `logs/triage.jsonl` (counts and ids only: no sender, subject or text),
and the model call is a normal `ws run` under the job name `NAME-model`, so it also appears in `ws runs` and counts
against the monthly cap. A run that finds nothing new makes no model call and costs nothing. The caps above bound a bad
day.

Measured on November 22, 2026 (one synthetic thread, Claude Code with a small model on the subscription login): about 31,000
tokens in and 470 out for one call, US$0.0065 at list price. Nearly all of the input is Claude Code's own context (its
system prompt, tools and your user-level instructions), not the thread, so a run's `run_tokens` is sized for that: 400000
is about twelve calls. A runner with a smaller context (an API model through OpenCode, a local model) costs far less per
call; the run records show which is which.

## The model

The model is one setting, `[triage.model] runner`. The default is the `claude-code` runner and a small model (your
subscription login, no extra money), which is the fallback because it needs no key; it is also the dearest in tokens,
since Claude Code sends its own context with every call. Change `[triage.model]` to use something else:

| You have | Set |
|---|---|
| An API key (the cheapest in tokens) | `runner = "anthropic-api"`, `model = "MODEL-ID"`, `[[triage.model.secrets]]` (`name`, `host = "api.anthropic.com"`), and `runner_options` `price_in_usd_per_mtok` and `price_out_usd_per_mtok` (ws ships no prices) and optionally `max_tokens` (default 400). One POST per call, no Claude Code. |
| An API model through OpenCode | `runner = "opencode"`, `model = "provider/model"`, and `[[triage.model.secrets]]` (`name`, `host`) for the key, leased through the broker as a job's `[[secrets]]` are |
| A local model | `runner = "opencode"`, `model = "ollama/NAME"` and `runner_options.provider` (see the job file keys in `ws.jobs`) |

`anthropic-api` calls the Messages API directly: the prompt and voice are the `system` text, the thread (marked untrusted
data) is the one user message, and there are no tools. The key never reaches ws: the broker leases a loopback address that
forwards to the host and adds the key, ws sends a placeholder, and the lease is released after each call. Cost is counted
from the reply's usage at your prices; `allow_unpriced = true` skips the prices and leaves only the token caps. It does
not go through `ws run`, so the call is not in `ws runs` or the monthly cap; the run's `budget_usd`, `run_tokens` and
`max_calls` hold, and a call whose worst case (the thread plus `max_tokens`) would pass what is left of `budget_usd` is
not made. Counted by characters (4 to a token), one call is about 700 tokens in for a short thread and about 2,100 for
one at its cap, with a 1,500-character prompt, the voice and the 1,500-token thread cap; the API's own count is in the
run record.

For `claude-code` and the OpenCode runners the model call goes through the same runner path as any job.

`max_turns = 1` is set for Claude Code. OpenCode has no turn limit flag, so for an OpenCode model the only bound is the
run's own timeout; the thread still goes in as marked untrusted data and the answer is parsed and checked the same way.

## Schedules

`every Nm HH:00-HH:00` runs every N minutes (N from 5 to 30) only from the first whole hour to the second, both ends
included: `every 30m 05:00-22:00` is 05:00, 05:30, 06:00, up to 21:30, then 22:00. The window doesn't cross midnight.
The systemd timer gets two `OnCalendar=` lines for it; launchd gets one calendar entry for each run.

## Limits

- A search returns the newest 50 messages. More than that in one go is reported as `more_than_one_search_page` in the
  run's notes; the first run of a very full mailbox is the likely case.
- A thread is the new messages of one run, grouped by the provider's thread id (Gmail) or the first message of the
  References chain. Earlier mail in the thread that isn't in the Inbox isn't seen. A reply goes against the newest
  message.
- `ws.mail` has no public call for a whole message or the mailbox's own address, so this module uses two of its private
  reads (`Mail._fetch`, `Mail._user`), both in `MailAdapter`. A public `fetch_raw` and `address` there replace them.
- A mailbox's `class` is checked against the policy before any mail is read; leave it out and nothing is checked, so name
  it for any mailbox that belongs to a walled area (and better, don't list that mailbox here at all).

## Exit codes

0 ran (flags are in the summary, not the exit), 1 a mailbox failed, 2 the settings, rules or a mailbox name couldn't be
used, 3 refused (a mailbox whose class isn't full access). A run that finds the job already running exits 0 and does
nothing.
