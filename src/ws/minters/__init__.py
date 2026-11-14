"""Minters: the broker turns a long-lived key it holds into a short-lived, narrowed credential for one run.

A registry entry with `kind = "<minter>"` names a minter in this folder. The broker reads the entry's extra keys through
the minter's `parse(path, name, table)` (which raises SecretsError for a bad key, so the whole registry is refused), and
answers a socket op by calling the minter with the stored key. The minter never logs, prints or returns the long-lived
key; what it returns is the short-lived credential (a token or a certificate) and its expiry.

The contract, per module: `KIND` (the registry `kind`), `OP` (the socket op), `parse(path, name, table) -> options`
(a frozen object), and the op's own function, documented in the module. A minter that can revoke what it issued also
offers `revoke(...)`, which the broker calls on `ws elevate off`.

Values a minter handles are short-lived bytes in the broker's process; the credential it returns goes to one caller,
over the broker's socket, and is never written to the audit log.
"""
