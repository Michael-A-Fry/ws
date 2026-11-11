---
status: accepted
date: 2026-11-08
decision-makers: maintainer
---

# The gate judges MCP tool calls

## Context and Problem Statement

An MCP tool is named `mcp__server__tool` and takes arguments of any shape. A session can reach files, hosts and other
programs through one as well as through the built-in tools, so the same policy should decide both: which tools may run,
which need a person's say-so, and which paths an argument may name.

## Considered Options

- Leave MCP tools to the runner's own permission prompt.
- Allow MCP tools unless `deny_tools` names them.
- Ask for any MCP tool the policy doesn't name, take names and globs in `deny_tools`, and read the arguments.

## Decision Outcome

Chosen option: the third. `deny_tools` takes `mcp__server__tool` and globs, checked when the policy loads (a bad entry
refuses the whole file). A new `allow_mcp` list names the tools a session may call outright; the rest are asked for with
the same single-use approval as `ask_commands`, and the ask is logged as an ask. `deny_tools` beats `allow_mcp`. When
the policy has no `allow_mcp`, only the tools `ws mcp serve` offers are allowed. Path-like arguments go through
`deny_read`, and through the protected-write check when the tool's name says it writes; arguments past a fixed depth or
count are a deny. Claude Code's hook matcher gains `mcp__.*`. OpenCode's payload can't tell an MCP tool from a
built-in one, so it gets only a native `deny_tools` rule for a literal server name.

### Consequences

- Good: a new MCP server is a question for the person, not a silent grant, and a denied folder stays denied whichever
  tool reads it.
- Bad: each unnamed MCP call needs an approval until its tool is in `allow_mcp`. Reading arguments by their key and shape
  can over-block a value that only looks like a path, and misses a path in a key it can't recognise.
- Bad: the default allow trusts the server name `ws`. A project that registers its own server under that name gets the
  same three tool names allowed; set `allow_mcp = []` to turn the default off.

### Confirmation

`tests/test_gate_mcp.py`.
