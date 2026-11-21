# Mail

Several mailboxes for triage: search, read, label and draft replies over IMAP, through the secrets broker. Nothing ws
offers sends, deletes or moves mail. The modules are `ws.mail` (the calls), `ws.mail_mcp` (the five tools),
`ws.mailcmd` (`ws mail`) and `ws.minters.imap` (the broker's fixed operations).

## Why it's shaped like this

Mail is untrusted input: anyone can send text that tries to steer the model reading it (indirect prompt injection). So
what can be done is fixed in code before any mail is read:

- The mailbox password lives only in the broker. No agent, job or MCP server ever holds it. An app password also
  sends mail over SMTP, so it must never leave the broker.
- The broker runs a whitelist of IMAP operations (list, search, fetch, label, draft). Any other command is refused.
  **The no-send guarantee is ws's broker whitelist, not the provider's:** Gmail would accept the same password for
  SMTP.
- The tools take narrow arguments. A draft takes the mailbox, the uid it replies to and the body, nothing else.
  Recipients, subject and threading come from the original. An argument a tool doesn't name is refused.
- A message comes back as plain text between two marker lines, as data. Text in the mail that looks like a marker is
  defused.

## Config

Each mailbox is one profile table and one secrets registry entry. `ws mail add` writes the profile table and prints
the registry entry for you to add; ws never writes the registry.

Profile (`profile.toml`):

```toml
[mail.work]
secret = "mail-work"  # the registry name
kind = "gmail"        # or "imap"; gmail means Gmail search syntax and Gmail labels
```

Secrets registry:

```toml
[mail-work]
kind = "imap"
host = "imap.gmail.com"
user = "someone@example.com"
# port = 993                          implicit TLS only
# allow = ["read", "label", "draft"]  narrow what this mailbox may do
# drafts_folder = "Drafts"            only when the server marks no folder \Drafts
```

Drafts go to the folder the server marks `\Drafts`, else `drafts_folder`. With neither, a draft is refused, never
guessed.

## The one Gmail step only you can do

1. Turn on 2-Step Verification for the Google account.
2. Create an app password (Google Account, Security, App passwords) and copy it.
3. Run `ws mail add work --host imap.gmail.com --user someone@example.com --kind gmail`. It opens a prompt window
   (`ws secret ask`); paste the app password there. ws logs in read-only and lists the folders it found.

Other providers work the same way with their IMAP host and an app password, if they offer one.

## Commands

| Command | What it does |
|---|---|
| `ws mail add NAME --host H --user U [--kind gmail] [--port P]` | Writes `[mail.NAME]` (secret `mail-NAME`). If the registry lacks that entry it prints the block to add and exits 1. Refuses an entry for another host, user or port (exit 2). Asks for the password when the store has none, then logs in read-only and prints the folders and the drafts folder (exit 1 when none is found). Prints the `[connectors.mail]` table when the policy lacks it. |
| `ws mail check` | Logs in read-only to every mailbox: ok, or the refusal code. Exit 1 when any fails or there are none. |
| `ws mail rm NAME` | Takes `[mail.NAME]` out of the profile. The registry entry and the stored password stay; it prints how to remove them. |
| `ws mail serve` | The five tools over stdio (MCP). A runner starts it through the connector. |

## Tools

Plug them into a session with this policy table (`ws mail add` prints it), then `ws connector launch -c mail -- claude`:

```toml
[connectors.mail]
command = ["ws", "mail", "serve"]
read_only = false
tools = ["mail_list_mailboxes", "mail_search", "mail_get", "mail_label", "mail_draft"]
```

Add `class = "..."` to keep mail away from sessions whose class may not read it (see the
[policy](policy.md#connectorsname)).

| Tool | Arguments | Returns |
|---|---|---|
| `mail_list_mailboxes` | none | The mailbox names and kinds. |
| `mail_search` | `mailbox`, optional `query`, `since` (YYYY-MM-DD), `unseen` | INBOX uids, newest first, at most 50, and the total. |
| `mail_get` | `mailbox`, `uid` | The message as delimited plain text: From, To, Subject, Date, Message-ID, then the body (capped). Reading doesn't mark it seen. |
| `mail_label` | `mailbox`, `uid`, `label` | Adds one of `ws/needs-reply`, `ws/fyi`, `ws/waiting`, `ws/low`. Nothing is removed or moved. |
| `mail_draft` | `mailbox`, `reply_to_uid`, `body` | Saves a reply in the drafts folder. Its first line is `[draft by ws: review before sending]`. |

A uid carries its mailbox and the folder's UIDVALIDITY (`work:7-42`). Another mailbox's uid is refused, and so is one
from before the folder was rebuilt. A failure is a tool result with `isError`, holding the reason and a hint, never
server text or a password.
