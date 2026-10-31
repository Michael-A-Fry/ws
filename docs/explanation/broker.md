# The secrets broker

The broker is the rule that an AI session never sees the value of a secret. It asks a small program for the use of a
key, and that program adds the key to the request at the one host the key is allowed for.

## The rule

The agent holds a placeholder key and a local address. It never holds the real key, so it can't print it, log it, send
it to another host or leave it in a transcript.

## Why it exists

A key in an environment variable or a file is readable by every program the session starts, and by the session itself.
A prompt-injected agent can then copy it out. If the session never has the value, there is nothing to copy.

## How ws enforces it

The code is `src/ws/broker.py` (the broker) and `src/ws/secrets.py` (the registry and the store).

1. **The registry.** `$WS_HOME/secrets.toml` names each secret and where it may be used: the hosts, the header the host
   expects and the purposes. The registry isn't secret.
2. **The store.** One file per name, mode 0600, in a folder of mode 0700. `ws secret set NAME` reads the value from a
   hidden prompt (or from standard input) and writes it. Nothing prints, logs or puts a value in an error message.
3. **The lease.** The runner asks the broker over a Unix socket for `request(name, purpose, host)`. The broker checks the
   caller, the registry and that a value is set. It returns a short-lived lease: a base URL on `127.0.0.1` and a header
   name. A lease is for one host, expires after 1 hour by default and can be released.
4. **The proxy.** The runner points its tool at the lease URL. The lease proxy strips any credential header the tool
   sent, adds the real one and forwards the request only to `https://<host>` for that lease. Responses stream back as
   they arrive.

The broker does no TLS interception and installs no certificate authority. The tool speaks plain HTTP to `127.0.0.1`,
and the broker speaks TLS to the real host. It fails closed: an unset name, a loose store, an unknown host or an expired
lease is a refusal, never a request without the credential or to a different host.

Callers are checked with `SO_PEERCRED` on Linux and `LOCAL_PEERCRED` on macOS. Only the broker's own user and members of
`--group` are served. `ws run` leases each secret named in a job file and releases the leases whatever happened,
including Ctrl-C.

### The secret wall

By default the store belongs to you, so a program that runs as you can read the files. `ws secret wall` prints the
one-time `sudo` steps that move the store to its own system user (`ws-broker`) and put a root-owned copy of ws beside
it. After that, the agent's user can't read the store at all. The wall is built for Linux with systemd. On macOS and
WSL2 it's not built yet, and `ws doctor` (WS104) stays at warn.

## What the broker doesn't stop

- **A same-user store.** Until you run the secret wall, a program running as you can read the store files. WS104 warns.
- **A secret the agent already has.** The broker can't take back a key that's in the environment, a file the agent can
  read or its prompt.
- **A value used for its own host.** A lease lets a session make requests to the allowed host, so it can use the key
  there. Scope the registry to the narrowest host and purpose.
- **Secrets the broker doesn't read yet.** The `sops-age` role is named in the profile and checked by `ws doctor`, but
  the broker doesn't read from it. Use `ws-secret`.
- **Notify caps.** The daily push cap counts ws's own sends only. Notify leases aren't counted in the broker.

See [Known limits](known-limits.md).

## Do it

Follow [Store a secret](../how-to/store-a-secret.md).
