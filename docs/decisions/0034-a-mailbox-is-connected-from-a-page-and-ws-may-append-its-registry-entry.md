---
status: accepted
date: 2026-11-22
decision-makers: maintainer
---

# A mailbox is connected from a page, and ws may append its registry entry for a known provider

## Context and Problem Statement

Adding a mailbox took a shell. `ws mail add` printed a registry block for the person to paste into their own
file, then opened a tmux prompt window for the app password. A person on a phone, or on a plain ssh login with no tmux,
couldn't finish. Gmail also shows an app password in four groups with spaces ("abcd efgh ijkl mnop"), and its IMAP
login refuses the value with the spaces left in, so a correct paste failed with no reason given. How does a person
connect a mailbox from a page, with no step that needs a shell?

## Considered Options

- Keep the CLI as the only way in, and document the steps better.
- A `ws connect` flow ([0033](0033-a-setup-step-is-data-walked-by-one-engine.md)) for each mail provider.
- One call, `ws.mailconnect.connect`, that any page (ws's own `ws mail connect --web`, or a person's own hub) posts a
  card to.

## Decision Outcome

Chosen option: one call that pages post to. The connect engine walks steps in a terminal only and has no page yet; when
it gets one, a mail flow can call `connect` as its verify step.

- **Check before store.** The typed value is tried with one read-only login and LIST before anything is written. A
  refusal stores nothing, so a value that works is never replaced by one that doesn't.
- **ws may append a registry entry, only for a known provider.** For a host in `PROVIDERS` (Gmail, Outlook, iCloud),
  a missing `mail-NAME` entry is appended after the login works. ws never edits or removes an entry, and for any other
  host it returns the block for the person to add. This replaces "ws never writes the registry" for this one case:
  a step ws can do itself, it does.
- **Tidy the value for its host.** A minter may refuse or tidy a value before it's stored (`clean_value`). For a
  Gmail host every whitespace character is dropped and the rest must be 16 letters. Other hosts keep the value as
  typed.
- **Say why, in ws's words.** A refused login is a typed reason (`bad_credentials`, `app_password_required`,
  `imap_off`, `web_login_required`) taken from the server's refusal inside the minter. A page shows ws's own sentence
  for each reason; the server's text never leaves the minter.
- **Stop before a lockout.** Three refused logins for one account (host and login, whatever the mailbox is named)
  in 15 minutes stop the next try before it dials. The check and this try's mark are one locked step, so parallel
  posts can't all pass. The count is a list of times in a state file, pruned by age, so it can't go stale.

### Consequences

- Good: a mailbox can be connected from a phone with no shell step, and a wrong paste says what to fix.
- Good: the password goes from the page's form to the store and nowhere else; it's in no result, error, state file
  or log.
- Bad: the process serving the page holds the value for the length of one test login. That process doesn't read
  mail: everything that reads mail still goes through the broker, which alone holds the stored value.
- Neutral: providers outside `PROVIDERS` still need the person to add the registry block.

### Confirmation

`tests/test_mailconnect.py` connects through a real broker to a fake TLS IMAP server and checks: a refused password
stores nothing and keeps the value that works; each refusal has its own words and never the server's text; the
fourth try inside 15 minutes is stopped before it dials; an entry is added only for a known provider's host; and the
password is in no file outside the store. `tests/test_mailweb.py` checks the page refuses a post without its token or
from another Origin, and that no reply or printed line carries the password.
