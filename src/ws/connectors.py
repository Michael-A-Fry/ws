"""Connectors: plug a zone's MCP server (`[connectors.NAME]` in the policy) into one run or one session, and log each call.

A connector is a third-party-app style link to a service that speaks MCP: an `https` address or a stdio command, the
data class of what it returns, a tool allowlist, and the name of the secret that opens it. ws.policy parses it; this
module wires it in. What a session that names a connector gets, and nothing more:

  * The runner's MCP config, written for that run or that launch only (`--mcp-config` with `--strict-mcp-config`, so no
    other server the person's own config names comes along). A connector with a `token` is reached through a broker
    lease (ws.broker): the config holds the lease's loopback address, the broker adds the token on the way out, and
    the runner never holds the value. The lease is for that one host, and ends with the run or the launch.
  * `--allowedTools` with the connector's allowlist (`mcp__NAME__TOOL`), and no other tool of that server. The gate's
    `allow_mcp` holds the same names (ws.policy derives it), and the connector's own PreToolUse hook (`ws connector
    audit`, written by ws.adapters.claude_code whenever the policy has connectors) refuses any tool of a connector's
    server that is not on its list, whoever started the session.
  * A refusal before anything starts when the session's class may not read the connector's class: the same rule
    memory uses (ws.memory `readable_by_caller`): the same class, or one whose `readable_by` names the session's.

Every MCP call to a connector's server is one line in `<state>/logs/connector-calls.jsonl`: the agent, the session,
the connector, the tool, allow or deny, the rule that decided, and the argument names (never their values). A line
that cannot be written is a deny: the audit is part of the allow.

`ws connector launch` is how a person starts an interactive session with one: it is an admin command (ws.agentsession),
so a session cannot hand itself a connector. Claude Code only for now; another runner is refused by name.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets as _random
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ws import broker, jsonl, output, paths, policy
from ws.output import WsError

COMMAND = "connector"

LOG = "connector-calls.jsonl"
SCHEMA_VERSION = 1
PURPOSE = "connector"  # the purpose a connector's token is leased for (a registry entry may limit a name to it)
AGENT_ENV = "WS_CONNECTOR_AGENT"  # who the audit line names: a job's name, or the `--agent` a launch was given
MAX_ARG_KEYS = 32
DENY = 2  # the hook's exit for a refused call (Claude Code shows its stderr to the model)


@dataclass
class Wiring:
    """What a runner is started with for its connectors: the extra argv, the tools to allow, and the leases to end."""

    argv: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    leases: list[broker.Lease] = field(default_factory=list)


def log_path() -> Path:
    return paths.state_dir() / "logs" / LOG


def session_class(pol: policy.Policy, cwd: str | os.PathLike | None) -> policy.Class | None:
    """The class of a session working in `cwd` (as ws.run sees a job): None with no `[classes]`, the folder's class,
    or the built-in `unclassified` when there is no folder."""
    if not pol.classes:
        return None
    if not cwd:
        return policy.UNCLASSIFIED
    return policy.class_of(pol, os.path.realpath(Path(cwd).expanduser()))


def refusal(pol: policy.Policy, conn: policy.Connector, cls: policy.Class | None) -> str | None:
    """Why a session of class `cls` may not have `conn`, or None. A connector with no class is anyone's."""
    if not conn.cls or cls is None:
        return None
    if cls.name == conn.cls or pol.classes[conn.cls].readable_by_class(cls.name):
        return None
    return (
        f"connector {conn.name} returns class {conn.cls} data, and a session of class {cls.name} may not read it "
        f"([classes.{conn.cls}] readable_by)"
    )


def pick(pol: policy.Policy, names: Iterable[str], cls: policy.Class | None) -> list[policy.Connector]:
    """The connectors named, in order, once each. Raises WsError `refused` (exit 3) for one the policy does not have or
    the class may not read, and `usage` for none."""
    names = list(dict.fromkeys(names))
    if not names:
        raise WsError("usage", "name at least one connector", "ws connector list shows them", 2)
    out = []
    for n in names:
        conn = pol.connectors.get(n)
        if conn is None:
            known = ", ".join(sorted(pol.connectors)) or "none"
            raise WsError("refused", f"no connector {n!r} in the policy (known: {known})", "add [connectors.NAME]", 3)
        if why := refusal(pol, conn, cls):
            raise WsError("refused", why, "run it from a folder whose class may read it, or widen readable_by", 3)
        out.append(conn)
    return out


def lease_host(url: str) -> tuple[str, str]:
    """(host as the broker's registry names it, the path and query to keep) for a connector's url."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower() + (f":{u.port}" if u.port else "")
    return host, (u.path or "/") + (f"?{u.query}" if u.query else "")


Lessor = Callable[[str, str], broker.Lease]  # (secret name, host) -> a lease


def default_lessor(ttl_s: float | None = None, sock: Path | None = None) -> Lessor:
    """Leases from the user's broker, for `PURPOSE`. A broker that is not running is started once, as ws run does."""
    from ws import secrets as wsecrets

    where = sock or wsecrets.socket_path(os.environ)

    def lease(name: str, host: str) -> broker.Lease:
        try:
            return broker.request(name, PURPOSE, host, socket=where, ttl_s=ttl_s)
        except broker.BrokerUnavailable as e:
            from ws import run

            _, mend = run.RECOVERIES[broker.BrokerUnavailable]
            if not mend(where, e)[1]:
                raise
            return broker.request(name, PURPOSE, host, socket=where, ttl_s=ttl_s)

    return lease


def server_entry(conn: policy.Connector, lessor: Lessor, wiring: Wiring) -> dict:
    """One `mcpServers` entry for Claude Code. A token is leased (and the lease added to `wiring`)."""
    if conn.command:
        return {"type": "stdio", "command": conn.command[0], "args": list(conn.command[1:])}
    if not conn.token:
        return {"type": "http", "url": conn.url}
    host, rest = lease_host(conn.url)
    try:
        lease = lessor(conn.token, host)
    except broker.BrokerError as e:
        code = getattr(e, "code", type(e).__name__)
        raise WsError(
            "refused",
            f"connector {conn.name}: the broker gave no lease for {conn.token} at {host} ({code})",
            f"check the registry allows {conn.token} at {host}, and that it has a value (ws secret list)",
            3,
        ) from e
    wiring.leases.append(lease)
    return {"type": "http", "url": lease.base_url.rstrip("/") + rest}


def wire(conns: list[policy.Connector], folder: Path, lessor: Lessor) -> Wiring:
    """Write the MCP config for `conns` into `folder` (0600) and say how to start the runner with it. On a failure the
    leases already taken are released."""
    wiring = Wiring()
    try:
        servers = {c.name: server_entry(c, lessor, wiring) for c in conns}
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        cfg = folder / f"mcp-{_random.token_hex(6)}.json"
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f, indent=2)
    except BaseException:
        release(wiring)
        raise
    wiring.argv = ["--mcp-config", str(cfg), "--strict-mcp-config"]
    wiring.tools = [t for c in conns for t in c.tool_names()]
    return wiring


def release(wiring: Wiring) -> None:
    for lease in wiring.leases:
        with contextlib.suppress(broker.BrokerError):
            broker.release(lease)
    wiring.leases.clear()


def narrow(conns: list[policy.Connector], asked: list[str]) -> list[str]:
    """A job's own `allowed_tools` with the connectors' allowlists added. Raises ValueError for a tool of a connector's
    server that its allowlist does not name (least agency: the allowlist is the most a run gets)."""
    allowed = {t for c in conns for t in c.tool_names()}
    prefixes = {f"mcp__{c.name}__": c for c in conns}
    for t in asked:
        for pre, c in prefixes.items():
            if t.startswith(pre) and t not in allowed:
                raise ValueError(f"runner_options.allowed_tools: {t} is not in [connectors.{c.name}] tools")
    return list(dict.fromkeys([*asked, *sorted(allowed)]))


# --- the hook: one audit line per call, and the allowlist enforced -------------------------------------------------


def decide(pol: policy.Policy, tool: str) -> tuple[policy.Connector | None, bool, str]:
    """(the connector whose server `tool` belongs to, allowed?, the rule that said so)."""
    for conn in pol.connectors.values():
        pre = f"mcp__{conn.name}__"
        if tool.startswith(pre):
            bare = tool[len(pre) :]
            if bare in conn.tools or "*" in conn.tools:
                return conn, True, f"[connectors.{conn.name}] tools: {bare if bare in conn.tools else '*'}"
            return conn, False, f"[connectors.{conn.name}] tools does not name {bare}"
    return None, True, "not a connector's tool"


def audit(payload: Mapping, pol: policy.Policy, env: Mapping[str, str]) -> tuple[bool, str]:
    """Judge one PreToolUse payload and write its audit line. (allowed?, the message for a deny). A line that cannot
    be written is a deny."""
    tool = payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else ""
    conn, ok, rule = decide(pol, tool)
    if conn is None:
        return True, ""
    args = payload.get("tool_input")
    keys = sorted(str(k) for k in args)[:MAX_ARG_KEYS] if isinstance(args, dict) else []
    rec = {
        "agent": env.get(AGENT_ENV) or payload.get("agent_type") or "session",
        "session": payload.get("session_id") if isinstance(payload.get("session_id"), str) else "",
        "connector": conn.name,
        "tool": tool,
        "decision": "allow" if ok else "deny",
        "rule": rule,
        "arg_keys": keys,
    }
    try:
        jsonl.append(log_path(), jsonl.stamped(rec, SCHEMA_VERSION))
    except (OSError, jsonl.LockTimeout) as e:
        return (
            False,
            f"ws connector: the audit line for {tool} could not be written ({type(e).__name__}), so it is refused",
        )
    return ok, "" if ok else f"ws connector: {tool} refused: {rule} (least agency: ask the person to add it)"


def cmd_audit(args) -> int:
    """`ws connector audit`: the PreToolUse hook. Fails closed: an unreadable payload or policy is a deny."""
    try:
        with paths.pinned_by(args.pin):
            payload = json.loads(sys.stdin.read() or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            ok, why = audit(payload, policy.load(Path(args.policy) if args.policy else None), os.environ)
    except (ValueError, OSError, policy.PolicyError, WsError) as e:
        print(f"ws connector: refused, the call could not be judged ({type(e).__name__})", file=sys.stderr)
        return DENY
    if not ok:
        print(why, file=sys.stderr)
        return DENY
    return 0


def hook_group(pol: policy.Policy, ws_cmd: list[str], policy_file: Path, pin: paths.Pin, timeout: int) -> dict | None:
    """The PreToolUse matcher group for every connector's server, or None when the policy has none. The hook reads the
    same policy file the gate's own hook does."""
    if not pol.connectors:
        return None
    import shlex

    argv = [*ws_cmd, "connector", "audit", "--policy", str(policy_file), *pin.args()]
    cmd = f"{shlex.join(argv)} || exit {DENY}"
    names = "|".join(sorted(pol.connectors))
    return {
        "matcher": f"mcp__({names})__.*",
        "hooks": [{"type": "command", "command": cmd, "timeout": timeout, "onFailure": "block"}],
    }


# --- the commands -----------------------------------------------------------------------------------------------------


def cmd_list(args) -> int:
    pol = policy.load()
    cls = session_class(pol, os.getcwd())
    rows = [
        {
            "name": c.name,
            "transport": "command" if c.command else "http",
            "class": c.cls or None,
            "read_only": c.read_only,
            "token": c.token or None,
            "tools": list(c.tools),
            "readable_here": refusal(pol, c, cls) is None,
        }
        for c in pol.connectors.values()
    ]
    if args.json:
        output.result({"connectors": rows})
    elif not rows:
        print("no connectors in the policy ([connectors.NAME])")
    else:
        for r in rows:
            here = "" if r["readable_here"] else "  (not readable from this folder)"
            print(f"{r['name']}: {r['transport']}, class {r['class'] or '-'}, {len(r['tools'])} tools{here}")
    return 0


def _agent_of(cmd: list[str]) -> str:
    for i, a in enumerate(cmd):
        if a == "--agent" and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith("--agent="):
            return a.partition("=")[2]
    return "session"


def cmd_launch(args) -> int:
    """`ws connector launch -c NAME -- claude ...`: `ws gate launch` with the connectors wired in, for this launch only."""
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd or os.path.basename(cmd[0]) != "claude":
        raise WsError("usage", "connectors are wired for Claude Code only", "ws connector launch -c NAME -- claude", 2)
    pol = policy.load()
    conns = pick(pol, args.connector or [], session_class(pol, os.getcwd()))
    from ws import selfcmd

    wiring = wire(conns, paths.state_dir() / "run" / "connectors", default_lessor())
    try:
        argv = [*selfcmd.argv(), "gate", "launch", "--runner", "claude-code", "--", *cmd, *wiring.argv]
        argv += ["--allowedTools", ",".join(wiring.tools)]
        env = {**os.environ, AGENT_ENV: _agent_of(cmd)}
        return subprocess.run(argv, env=env, check=False).returncode
    finally:
        release(wiring)
        with contextlib.suppress(OSError):
            Path(wiring.argv[1]).unlink()


def register(sub) -> None:
    c = sub.add_parser("connector", help="zone MCP servers ([connectors.NAME]): list, launch a session with one, audit")
    cs = c.add_subparsers(dest="connector_command", required=True)
    ls = cs.add_parser("list", help="the policy's connectors, and whether a session here may read each")
    ls.set_defaults(func=cmd_list)
    la = cs.add_parser("launch", help="start Claude Code (through ws gate launch) with the named connectors wired in")
    la.add_argument("-c", "--connector", action="append", metavar="NAME", help="a connector to plug in (repeatable)")
    la.add_argument("cmd", nargs="...", help="-- then the runner's command line")
    la.set_defaults(func=cmd_launch, raw=True)
    au = cs.add_parser("audit", help="hook entry: judge one MCP call against its connector's allowlist and log it")
    au.add_argument("--policy", metavar="FILE", help="the policy file the session's gate reads")
    paths.add_pin_option(au)
    au.set_defaults(func=cmd_audit, raw=True)
