# Asks

`ws ask` is one queue of questions across every session. A session that needs a person (a choice, a sign-off) adds an
ask and goes on; the person answers from any terminal, or from `ws home`; the session reads the answer back.

There is one store: `asks.jsonl`, the file `ws home` reads asks from (see [Stores](stores.md)). It's plain JSON lines, so
anything that reads JSON lines can show or answer asks, and replace this command.

```text
ws ask add --question "Ship it?" --option "Yes|tests are green" --option "No" --session build
ws ask list                      # open asks, oldest first
ws ask get ID
ws ask answer ID 2               # an option's number (1-based), its label in any case, or free text
ws ask wait ID --timeout 600     # block until answered
ws ask done ID                   # the session has acted on it (an open or answered ask)
ws ask withdraw ID               # the session no longer needs it (an open ask)
ws ask import OLD.jsonl --dry-run
```

A label (any case) is tried before a number, so an option labelled "2" isn't read as the second one. A free-text
answer is scanned like a question: a secret or a sensitive term refuses it (exit 1). The first `--option` is the
recommendation. An open ask with the same `--dedupe-key` returns its id and adds nothing. `--stdin` reads the ask from
standard input as one JSON object (`question`, `options`, `decision`, `repo`, `session`, `dedupe_key`,
`free_text_allowed`); a flag overrides it. `--cwd DIR` says where the ask comes from (default: here). `--json`
(global) gives one envelope. There is no "asked by" field: `session` says who asked.

## What an ask may hold (the wall)

An ask is stored plainly only when everything below says it may be.

- **Class.** The repo's data class is the strictest of the named repo's class and the working folder's class, both from
  the policy's `[classes]`. A repo is named by a label, so it takes the class of any class whose `repos` glob ends in that
  name; a name that's a folder of the working directory takes the working directory's class; any other name, and a path
  in no class, is the strictest. A policy with no `[classes]` classifies nothing, so nothing is held back; a policy that
  doesn't load holds everything back. Only `access = "full"` is stored plainly.
- **Terms.** Any hit on the sensitive-terms file redacts. The file is `WS_ASK_TERMS`, else `ask-terms.txt` in the ws
  config folder: one term per line, `#` comments, a plain term matches in any case as a substring, `=TERM` matches that
  exact case as a whole word. A missing or unreadable file redacts every ask (fail safe); a readable file with no term
  says there are none. Create it with one comment line to turn the default off.
- **Redaction.** A redacted ask stores no text: the question is `Open the session to see this ask.`, no options, no
  decision, no free text, no folder and no session name, and a dedupe key is stored as `sha256:...`. The repo and the
  tmux target are kept so the person knows where to go. `ws ask add` says why on standard error.
- **Secrets.** A token-shaped secret in an ask, an option, a dedupe key or an answer is refused (exit 1) and stored
  nowhere. The scan is the one `ws msg` uses.
- **Read again.** The class is checked again whenever an ask is read: one whose repo is no longer full shows as "Open the
  session to see this ask." in `list` and `get`, and `answer` is refused (exit 1).
- **Files.** The folder is made 0700 and the file 0600.

The old program's `flows_back` maps to the policy's `access`: `full` is `full`, `counts` is `walled`, `off` is
`counts-only`. The ask records it as `flows` (`full`, `counts`, `off`).

## The events

Each line has `schema` (1), `ts` (epoch milliseconds, as the OpenCode shim writes it), `at` (the same moment as RFC 3339
text), `event` and `id`. `added` carries the ask; `answered` carries `answer` (`text`, `option_index` or null, `via`,
`ts`). Status is folded from the events: open, then answered, then done; or open, then withdrawn. Lines from the OpenCode
shim (no `answer`, no `at`) fold the same way, and so do the rows of the older `ws-ask` program (`v` 1, `ts` as
RFC 3339 text), which are read with their times. New keys and event kinds were added to the file; none changed meaning.

## Wait, done, withdraw

`wait` polls the store each second and prints the answer (exit 0). At the timeout (default 540 seconds, under a
ten-minute tool limit) it prints `still open` and exits 3. An ask that was withdrawn or done with no answer prints its
status and exits 4. `done` and `withdraw` print the new status; on an ask that's already closed they write nothing and
print its status. An unknown id is exit 2 for all three.

## Import

`ws ask import PATH` converts an older asks file (`v` 1 lines with an RFC 3339 `ts`) into events here: `ts` becomes epoch
milliseconds, `v` is dropped, and the ask's own fields are kept. Run it twice and the count is the same: a row already
there (same event and id) is skipped. A row that isn't valid, or closes an ask that isn't there, is counted as skipped.
A row that doesn't say `redacted: false` comes in redacted, and a redacted row keeps its stored text (none). `--dry-run`
writes nothing, not even the folder. The older file isn't read after that.

## Notify

Notify isn't built. `ws.asks.NOTIFY_HOOK` is a function a later release or a local patch may set; `ws ask add` calls it
once for a new ask with the stored event (a redacted ask has no text in it). By default it does nothing. Because the
store is plain JSON lines, anything that reads them (a script, a page, a phone push) can replace this command without
a migration.

Exit codes: 0 fine, 1 refused (a secret, already answered, a wall, a store that can't be written), 2 usage or an
unknown id; `wait` also 3 (timed out) and 4 (closed with no answer).

## The older program

The default store is the file the older `ws-ask` program writes, so its open asks show up here and `answer` works on
them. Every write here takes the same `asks.lock` beside the store, so a check and the write after it (the dedupe check
in `add`, the "still open" check in `answer`) are one step against both programs. Rows written here have `schema` and
epoch-millisecond `ts`, which the older program's reader skips, so an older `ws-ask wait` doesn't see an answer given
with `ws ask answer`. Move every session to `ws ask` at the same time, and stop the older writers then.
