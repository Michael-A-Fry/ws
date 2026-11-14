# Use short-lived credentials

Give each run only the secrets it declares, in its own tier and area, and stop elevated ones at once when you need to.
To understand the rule first, read [The secrets broker](../explanation/broker.md).

## Before you start

- A working broker and at least one stored name ([Store a secret](store-a-secret.md)).

## Steps

1. Scope the name. Add a `tier` and an `area` to its table in `secrets.toml`:

   ```toml
   [deploy-token]
   hosts = ["api.example.com"]
   header = "authorization"
   tier = "worker"
   area = "home"
   ```

   The tiers, lowest first: `walled` (a walled class's own keys; nothing else reaches them), `worker` (build jobs),
   `lead` (reaches `worker` names too) and `lead-elevated` (reaches both; the names `ws elevate off` stops).

2. Write a run record before the run starts. Wave 1 has no launcher that does it for you, so write it yourself, as the
   broker's user, in your own terminal:

   ```sh
   ws broker run-record write 20261115T090000Z-a1b2c3 --area home --tier worker --name deploy-token --ttl 3600
   ```

   `--uid` limits the run to one caller. `--ttl` ends the run, and every lease under it, after that many seconds.

3. Lease under the run. The request names the run ID and nothing else about itself:

   ```sh
   ws broker request deploy-token --purpose run --host api.example.com --run 20261115T090000Z-a1b2c3
   ```

4. End the run. Its leases end with it:

   ```sh
   ws broker run-record rm 20261115T090000Z-a1b2c3
   ```

5. Copy the audit log off the machine. Set a destination under `[roles.secrets]` in the profile: a folder on a share
   mounted from another machine, or an `scp` target that uses a key with no passphrase prompt.

   ```toml
   [roles.secrets]
   audit_copy = "/mnt/offbox/ws-audit"
   ```

   Then `ws broker audit-copy` copies it as `broker-<this machine's name>.jsonl`. Run it daily from cron or a systemd
   timer, as the broker's user. (A `ws schedule` job file can't run it yet: the job layout lists the commands it may
   schedule, and this isn't one of them.)

6. Use the kill switch. `ws elevate off` refuses every `lead-elevated` name at once and drops their live leases. Any
   session may run it. `ws elevate on`, from your own terminal, allows them again. `ws elevate status` says which.

## Mint GitHub tokens instead of storing one

The broker can hold a GitHub App's private key and mint 1-hour installation tokens, narrowed to the repos and
permissions a run asks for (never more than the entry allows). `python3 -m ws.minters.github_app setup-steps` prints
the browser steps to make and install the App and the registry table to add. Then point git at the helper, and run
`gh` through it, so the token never reaches the session's output:

```sh
git config --global credential.https://github.com.helper \
  '!python3 -m ws.minters.github_app credential --name github-app --run "$WS_RUN_ID" --repo ws'
python3 -m ws.minters.github_app gh --name github-app --run "$WS_RUN_ID" --repo ws -- pr create --fill
```

`ws elevate off` revokes upstream every token minted for an elevated App entry.

## Sign SSH certificates instead of handing out keys

The broker can hold an SSH certificate authority key and sign a session's own public key for 15 minutes, with the
principals the entry gives the run's tier:

```toml
[ssh-ca]
kind = "ssh-ca"
principals = { lead = ["ws-audit"], "lead-elevated" = ["ws-admin"] }
tier = "lead"
area = "home"
```

Store the CA key with `base64 -w0 ws_user_ca | ws secret set ssh-ca`. A session asks with
`python3 -m ws.minters.ssh_ca sign --name ssh-ca --run "$WS_RUN_ID" --key ~/.ssh/id_ed25519.pub`, which writes
`id_ed25519-cert.pub` beside the key. `python3 -m ws.minters.ssh_ca host-steps --name ssh-ca --user USER --principal P`
prints the `sshd` lines for a host to trust the CA. Run them only on hosts agents may reach: a host without the CA line
can't be opened by any certificate. There's no revocation; a certificate is short instead, and `ws elevate off` stops
new ones for elevated entries at once.

## Check it worked

- A lease for a name the run didn't declare is refused with `not_declared`, one line in `logs/broker.jsonl`.
- `ws broker status` shows `"elevated": false` after `ws elevate off`.

## If it fails

- *run_required*: the name is scoped, so send `--run` with a run ID that has a record.
- *run_refused*: no record, or one the broker can't trust. It must be 0600, in a 0700 folder, owned by the broker's
  user. A symlink is refused.
- *claim_refused*: the request named an area or tier. The broker reads those from the record only.
- *audit_failed*: the broker couldn't write its audit line, so it didn't give the lease. Check the `logs/` folder.
