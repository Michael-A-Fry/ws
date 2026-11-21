---
status: accepted
date: 2026-11-21
decision-makers: maintainer
---

# Lead-only lets the lead into a walled class, and fixed-kind cards cross the account wall by a spool

## Context and Problem Statement

Switching the walls on, with a class account per walled class and the person's home closed (0700), showed two gaps in
messaging. First, `inbound = "lead-only"` on a walled class refused every sender, the lead included, so it worked like
`off`. The launch file also refuses Claude Code's own cross-session messages (`crossSessionInbound`), so nothing could
reach a walled session at all, though the intent of lead-only is that the lead can. Second, a walled session's `done` or
`counts` card is meant to reach the lead, but the lead's message store is under the lead's home, which the class account
can't reach, so `ws msg send` from inside the class has nowhere to write.

## Considered Options

- Keep lead-only meaning "nobody" for a walled class, and make people use `allowlist` with the lead's session id. - Let
  lead-only mean what it says for a walled class: the lead, and nobody else. - For cards: a shared message database both
  accounts can write. - For cards: a spool folder, where one account may only drop and the other only takes. - For
  cards: a socket or service that relays them.

## Decision Outcome

Chosen option: lead-only lets a session whose role is `lead` into a walled class (with the walled-inbound kinds and the
class store as before), and every other sender is refused. `off` still refuses everyone, the lead included. `allowlist`
is obeyed as written. The role comes from a launch token only (`session.prepare_launch`), so a session can't claim it.
The person's own send (`ws msg send` with the per-boot token, which also carries role `lead`) isn't a lead session and
stays refused: the person works in a walled class through that class's own sessions.

For cards: a spool folder, `ws cards` (`src/ws/cards.py`). The admin makes `/var/spool/ws-cards` once: owned by the
lead's account, group `ws-cards` (the class accounts and the lead's account), mode exactly 3730, and both sides refuse
any other mode. Setgid gives each card the group `ws-cards`, so the lead can read it; the group has write and search but
not read, so a class can't list the folder or read another class's card; sticky stops a class deleting one. `drop`
`fchmod`s each card to 0640 whatever the umask and checks its group is `ws-cards`. A card is one of `msg.FIXED_KINDS`
with five keys in exact shapes (`session` a UUID, `at` `%Y-%m-%dT%H:%M:%SZ`), so it carries no free text. The lead's `ws
cards take` trusts the card's class only if the file's OS owner is that class's `os_user`, and needs group `ws-cards` on
the file. A card that fails any check is removed and reported as a count per reason code, never by its name or its text.
A shared database would give a class account write access to everything in it. A relay service is a process to run and
secure for something a folder's modes already give.

### Consequences

- Good: lead-only does what its name says, and a walled session's `done` reaches the lead across the account wall
  without any shared store.
- Bad: cards aren't delivered into the lead's inbox by the hook yet. The lead runs `ws cards take`.
- Bad: the spool is one more root-made folder for the admin to keep. Any mode but 3730 makes both sides refuse (fails
  closed).

### Confirmation

`tests/test_walls_on.py` covers lead-only with two walled classes (the lead in, a worker and the person's own send out,
no kind between the walled classes), the spool modes, the card shapes and reason codes, and the cold review's probe that
no planted text comes out of `take`. `tests/test_msg.py` and `tests/test_walls_flows.py` keep lead-only and off as
written in the message store and in `ws walls check`.
