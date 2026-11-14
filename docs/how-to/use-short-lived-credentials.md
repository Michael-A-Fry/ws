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

## Check it worked

- A lease for a name the run didn't declare is refused with `not_declared`, one line in `logs/broker.jsonl`.
- `ws broker status` shows `"elevated": false` after `ws elevate off`.

## If it fails

- *run_required*: the name is scoped, so send `--run` with a run ID that has a record.
- *run_refused*: no record, or one the broker can't trust. It must be 0600, in a 0700 folder, owned by the broker's
  user. A symlink is refused.
- *claim_refused*: the request named an area or tier. The broker reads those from the record only.
- *audit_failed*: the broker couldn't write its audit line, so it didn't give the lease. Check the `logs/` folder.
