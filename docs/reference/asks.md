# Asks

`ws ask` is one queue of questions across every session. A session that needs a person (a choice, a sign-off) adds an
ask and goes on; the person answers it, and the session reads the answer back.

There is one store: `asks.jsonl`, the file `ws home` reads asks from (see [Stores](stores.md)). It's plain JSON lines, so
anything that reads JSON lines can show asks, and replace this command.

```text
ws ask add --question "Ship it?" --option "Yes|tests are green" --option "No" --session build
ws ask list                      # open asks, oldest first
ws ask get ID
```

The first `--option` is the recommendation. An open ask with the same `--dedupe-key` returns its id and adds nothing.
`--stdin` reads the ask from standard input as one JSON object (`question`, `options`, `decision`, `repo`, `session`,
`dedupe_key`, `free_text_allowed`); a flag overrides it. `--cwd DIR` says where the ask comes from (default: here).
`--json` (global) gives one envelope. There is no "asked by" field: `session` says who asked.

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
  decision, no free text, and a dedupe key is stored as `sha256:...`. The repo and session are kept so the person knows
  where to go. `ws ask add` says why on standard error.
- **Secrets.** A token-shaped secret in an ask, an option, or a dedupe key is refused (exit 1) and stored
  nowhere. The scan is the one `ws msg` uses.
- **Read again.** The class is checked again whenever an ask is read: one whose repo is no longer full shows as "Open the
  session to see this ask." in `list` and `get`.
- **Files.** The folder is made 0700 and the file 0600.

The old program's `flows_back` maps to the policy's `access`: `full` is `full`, `counts` is `walled`, `off` is
`counts-only`. The ask records it as `flows` (`full`, `counts`, `off`).

## The events

Each line has `schema` (1), `ts` (epoch milliseconds, as the OpenCode shim writes it), `at` (the same moment as RFC 3339
text), `event` and `id`. `added` carries the ask; `answered` carries `answer` (`text`, `option_index` or null, `via`,
`ts`). Status is folded from the events: open, then answered, then done; or open, then withdrawn. Lines from the OpenCode
shim (no `answer`, no `at`) fold the same way, and so do the rows of the older `ws-ask` program (`v` 1, `ts` as
RFC 3339 text), which are read with their times. New keys and event kinds were added to the file; none changed meaning.

Exit codes: 0 fine, 1 refused (a secret, a store that can't be written), 2 usage or an unknown id.

## The older program

The default store is the file the older `ws-ask` program writes, so its open asks show up here. Every write here takes the
same `asks.lock` beside the store, so a check and the write after it (the dedupe check in `add`) are one step against both
programs. Rows written here have `schema` and epoch-millisecond `ts`, which the older program's reader skips, so an older
`ws-ask` doesn't see an ask added here. Move every session to `ws ask` at the same time, and stop the older writers then.
