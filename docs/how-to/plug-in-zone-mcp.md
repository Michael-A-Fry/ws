# Plug in a zone MCP server

Give one agent a service's tools, the way you'd plug a third-party app into an account: read-only by default, only
the tools you list, only for the sessions whose class may read what it returns, and one log line per call. The service
is any MCP server: an app of your own on your network, or a vendor's.

## Before you start

- The server, reachable over `https` (a server on this machine may use `http://127.0.0.1`), or a stdio command.
- If it takes a token: a working broker and the token stored under a name ([Store a secret](store-a-secret.md)). With
  a value ws made for you (`ws secret generate`, where your ws has it), nothing is pasted at all.
- A data class for what it returns, if it's private ([classes](../reference/policy.md#classesname)).

## Steps

1. Allow the token at the server's host, in `secrets.toml`:

   ```toml
   [ledger-token]
   hosts = ["ledger.example.test"]
   header = "authorization"
   format = "Bearer {value}"
   purposes = ["connector"]
   ```

   Leave out `tier` and `area` for now: a connector's lease carries no run id yet, so a scoped name is refused.

2. Add the connector to `policy.toml`. List only the tools the agent needs:

   ```toml
   [connectors.ledger]
   url = "https://ledger.example.test/mcp"
   class = "money"
   token = "ledger-token"
   tools = ["balances", "transactions_summary", "budgets"]
   description = "the household ledger, read-only"
   ```

   `read_only` is on unless you turn it off: a tool whose name says it changes something (`create_*`, `delete_*`,
   `send_*`, `pay_*`) is refused when the policy loads. The gate now allows `mcp__ledger__balances` and the rest, as if
   `[gate] allow_mcp` named them.

3. Check it from the folder the agent works in:

   ```sh
   ws connector list
   ```

   `not readable from this folder` means the folder's class may not read `money`. Add it to `readable_by` in
   `[classes.money]` if that's what you want, as a reviewed change.

4. Use it.

   - A job: add `connectors = ["ledger"]` under `[runner_options]`. `allowed_tools` gains the connector's tools; naming
     one of its tools that the list doesn't have refuses the job.
   - A session at your terminal: `ws connector launch -c ledger -- claude --agent planner`. A session can't run this
     command for itself.

5. Read the log: `logs/connector-calls.jsonl` in ws's [state folder](../reference/stores.md#json-lines-logs). One line
   per call, allowed or refused, with the agent, the tool, the rule that decided and the argument names. Values are
   never logged.

## What you get

- The runner is started with an MCP config for that run only (`--mcp-config`, `--strict-mcp-config`): your own MCP
  servers don't come along, and the connector isn't in any other session.
- The runner never holds the token. It talks to a broker lease on `127.0.0.1`, which adds the token on the way to that
  one host and ends with the run or the launch. The runner holds the lease address, as it holds its model lease, so the
  server's own log is the second record of who called it.
- A tool the list doesn't name is refused by the connector's hook, whoever started the session, and the refusal is
  logged.

## Swap it out

Nothing here is tied to one service or one runner. A connector is an address, a class, a token name and a tool list in
open TOML. To move to another server, change `url` (or `command`) and `tools`; to drop it, delete the table, and its
tools leave the gate's allow list with it. The wiring is written for Claude Code today; another runner is refused by
name until it has an adapter.
