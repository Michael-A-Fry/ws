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
  accounts can write. - For cards: a spool folder, where one account may only drop and the other only takes. - For cards:
  a socket or service that relays them.

## Decision Outcome

Chosen: lead-only lets a session whose role is `lead` into a walled class (with the walled-inbound kinds and the class
store as before), and every other sender is refused. `off` still refuses everyone, the lead included. `allowlist` is
obeyed as written. The role comes from a launch token only (`session.prepare_launch`), so a session can't claim it.

For cards: a spool folder, `ws cards` (`src/ws/cards.py`). The admin makes `/var/spool/ws-cards` once: owned by the
lead's account, group `ws-cards`, mode 1730. A class account can create a file there, but can't list the folder or
delete another account's file. A card is one of `msg.FIXED_KINDS` with five fixed keys and no text. The lead's `ws cards
take` trusts the card's class only if the file's OS owner is that class's `os_user`. A card that claims another class,
or carries any other key, is rejected and removed. A shared database would give a class account write access to
everything in it. A relay service is a process to run and secure for something a folder's modes already give.

### Consequences

- Good: lead-only does what its name says, and a walled session's `done` reaches the lead across the account wall
  without any shared store.
- Bad: cards aren't delivered into the lead's inbox by the hook yet. The lead runs `ws cards take`.
- Bad: the spool is one more root-made folder for the admin to keep. A wrong mode makes both sides refuse (fails closed).
