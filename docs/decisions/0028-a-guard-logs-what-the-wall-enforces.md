---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# A guard logs what the wall enforces, only where the wall is proven for the call

## Context and Problem Statement

Some refusals guard against what a command could do to the machine: a line whose program word the reader can't resolve,
a line that sets `IFS`, a write to a scratch file whose name only the shell knows. When the shell runs inside the OS
sandbox, the wall already holds those: writes are limited to the project and the folders the policy names, ws's own
files are read-only, and the `deny_read` folders are hidden. The refusal then costs a session a retry and adds no
safety. Other refusals are advice about the work, not the machine (a message that says a commit is pushed when it
isn't; a test skipped in an edit; a file that doesn't parse), and a person may want to see how often they fire before
they stop a session.

The wall isn't always there: native Windows has none, a machine may lack the binary, a session may run with the sandbox
off, a single call may be marked to run outside it, and a project's settings can put a variable in the session's
environment. Anything that stands down for the wall has to be told by something a project can't set, call by call, and
must fail closed to the refusal it replaced.

## Considered Options

- Leave every refusal as it is. - Let a policy switch any rule off. - Let each guard take `mode = "log"`, and let the
  gate stand down only where the wall is proven for that call, for guards and for the refusals the wall makes
  redundant. - Prove the wall once for the session, at the start.

## Decision Outcome

Chosen option: a per-guard `mode`, and one proof of the wall per call.

**`[guards.NAME] mode = "enforce" | "log"`.** The default is enforce. The policy reads the key and removes it before the
guard's own `config()` runs; any other value refuses the whole policy, naming the key. The policy keeps each guard's
mode (`policy.Guards.modes`).

**The effective mode is log only when log is asked for and the wall is proven for this call** (`gate.wall_gap` says ""
for proven, else one fixed word). All of these must hold, and each is something a project can't set:

- not native Windows (`native-windows`);
- Linux or WSL2 with `bwrap` on PATH, or macOS with `sandbox-exec` (`no-sandbox-binary`); nothing is spawned,
  `shutil.which` only;
- the session's environment carries ws's marker `WS_SANDBOXED=1`, which the settings ws wrote set (`sandbox-off`);
- the adapter's preflight ran for this payload and refused nothing, and no project settings file in the project folder
  or above it puts the marker, ws's gated-session marker or `PATH` in the session's `env` (`preflight`): a project that
  sets `WS_SANDBOXED=1` while the sandbox is off is a forged marker, not a wall;
- the call isn't marked to run outside the sandbox (`dangerouslyDisableSandbox` set; `unsandboxed-call`).

Computed once per call in `gate._check` (and for an event in `gate._event`), kept by a small object and asked for only
when a guard in log mode denies or a rule the wall makes redundant refuses, and passed to the guards as `Context.wall`.
In log mode a guard's deny becomes a note and a `would-deny` verdict, yielded by `guards.stream`; an approvable deny
asks for no approval and consumes none. Without the wall the guard refuses exactly as in enforce, and its verdict
carries the word.

**Rule refusals the wall makes redundant**, for command calls only (the sandbox wraps the shell, not the file tools),
use the same proof: a program word that holds a part ws can't read (`unknown-command-word`), a line that sets `IFS`
(`sets-ifs`) and a write to a path ws can't read, such as a scratch file `mktemp` made in the project (`unread-write`).
Each carries its kind in the type: `policy.Refusal` is a `str` with a `kind`, as `gate.Ask` is, and the `IFS` error
keeps its kind `unsupported` and gains a `rule`. Where the wall is proven, the line is read again past the refusal, so
every other rule still judges the rest of it (`deny_commands`, `deny_read`, protected writes, the `ask_commands` list,
the guards); a line that sets `IFS` is read with the setting renamed, after the same folding the reader applies, so a
spelling the plain text hides (full-width letters, a backslash-newline inside the name) is renamed too. Whatever else
the line holds that a rule refuses is a deny in every mode. A read through `<` that ws can't resolve isn't a write, and
stays a plain refusal. So does a write whose path spells a protected file or folder after a part ws can't read, and a
line that sets `IFS` where something could split on it (an expansion outside quotes, a substitution, a backtick, a
here-document, a `case`, or the setting inside quotes a nested shell runs): the words such a line runs aren't the words
ws read, so no rule about which program runs could be trusted.
A program word ws can't read stays a plain refusal as well wherever the policy sets any `deny_commands` or `ask_commands`
rule, or turns on a guard that judges shell commands in `enforce` mode: the wall holds where a line writes, not which
program it runs, and neither a rule nor a guard about a program can match a word ws can't read (`$X git add -A` is git
to the shell, not to `bulk-staging`). A line read past a refusal is read past it for `ask_commands` too, so a segment
after a scratch write still asks.

**A guard asked to log gives the runner no deny of its own** (`NATIVE_DENY`): a runner's rule can't be logged instead of
refused, and the hook, which always runs, judges the call. Every form `bulk-staging` blocks that a pattern can say is
in its `NATIVE_DENY`; `checkout-pin` has none, because what it denies depends on the folder and a pattern on the
command text can't see a folder.

**One counts-only line.** A hook run that let through a would-deny writes the existing verdict line to
`<state>/logs/gate.jsonl` through `gate._log_verdicts`: the session, the tool's name, and for each guard or rule
(`rule:KIND`) a `would-deny` with a count and the time. It holds no command, path or message text. The write is capped
by a named constant (`gate.VERDICT_MAX_BYTES`) and rotates as every ws log does. A log that can't be written never
changes the decision.

**A tool no adapter reads** is one Call of kind `other` made from its name (capped), allowed, and logged once. The hook
matches every tool (`claude_code.MATCHER` is `*`), so the call arrives; `record-calls` records `mcp` and `other` calls
too.

### Consequences

- Good: a sandboxed session stops paying a retry for a refusal the wall already covers, and a person can watch the prose
  guards before they enforce them.
- Good: nothing relaxes anywhere the wall can't be shown; the words in the log say which part of the proof failed.
- Good: a forged marker, a project `PATH`, a call marked outside the sandbox and a broken log each leave the refusal as
  it was.
- Bad: the proof reads the platform, `PATH` and the session's environment, so a replay would depend on the machine; `ws
  gate replay` and the corpus never prove the wall, and judge as with no wall.
- Bad: an unknown program word is let through in a proven wall only where nothing judges the program by name: no
  `deny_commands` or `ask_commands` rule, and every guard that reads shell commands in `log` mode. The template turns
  on `dev-server` in `enforce`, so out of the box this refusal stays a deny; a person who wants it logged sets
  `dev-server` to `log` too.
- Bad: a guard in log mode loses its native deny, so with the hook down the runner no longer refuses those forms. The
  hook's failure already blocks every call (`onFailure: block`).
- Bad: a redirect to a path that's nothing but a part ws can't read (`> "$F"`) is a write the wall bounds, so it's
  allowed in a proven wall, though `$F` could name a file ws protects. The wall, and not the protected-write rule,
  stops that write; a path that spells a protected name after the unread part is still a deny.

### Confirmation

`tests/test_guard_modes.py` runs the four neutral cases (a loop over a temp dir, a line that sets `IFS`, a message with
the sentence "Make sure the branch is pushed before you report" beside a release claim, a scratch write inside the
project) in a faked wall, then with the binary missing, the sandbox off, a forged marker and a call marked unsandboxed,
and checks the log line holds no command, path or text, an approvable deny consumes no approval, a log that can't be
written changes nothing, and WS018 warns where log can't apply. `tests/test_policy.py` pins the mode key and the
template. `tests/test_gate_unknown_tool.py` pins the `other` call and `tests/test_guard_native_deny.py` pins every form
a pattern can say. Principle P-guard-modes in `docs/reference/principles.md` names the check.
