"""`ws gate`: turn the policy into each runner's own config, and answer the hooks.

    ws gate init                                   write the commented default policy if there is none
    ws gate generate --runner R --out DIR          write R's native config into DIR (0 changes when it is current)
    ws gate check --runner R                       hook payload on stdin; exit 0 = allow, 2 = deny (reason on stderr)

`check` fails closed: a payload it cannot read, a policy that does not load, or any crash is a deny.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from ws import adapters, policy
from ws.adapters import Call

DENY = 2


def evaluate(pol: policy.Policy, call: Call, protect: tuple[str, ...] = ()) -> str | None:
    """The reason a call is denied, or None. The one place a neutral Call meets the policy."""
    if call.kind == "command":
        return policy.is_denied_command(pol, call.value, protect=protect)
    if call.kind == "read":
        return policy.is_denied_read(pol, call.value)
    if call.kind == "search":
        return policy.is_denied_search(pol, call.value)
    if call.kind == "write":
        return policy.is_protected_write(protect, call.value)
    if call.kind == "tool":
        return f"tool {call.value} is in deny_tools" if call.value in pol.gate.deny_tools else None
    raise ValueError(f"unknown call kind {call.kind!r}")


def check(runner: str, raw: str, pol: policy.Policy, protect: tuple[str, ...] = ()) -> str | None:
    """None to allow, else the reason. Raises on anything it cannot judge; cmd_check turns that into a deny."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("payload is not a JSON object")
    calls = adapters.get(runner).parse(payload)
    for call in [] if calls is None else [calls] if isinstance(calls, Call) else calls:
        reason = evaluate(pol, call, protect)
        if reason:
            return reason
    return None


def _policy_arg(args: argparse.Namespace) -> Path | None:
    return Path(args.policy).expanduser().absolute() if args.policy else None


def cmd_check(args: argparse.Namespace) -> int:
    try:
        policy_file = _policy_arg(args) or policy.policy_path()
        # ws's own files: the policy it reads, the control files beside it, and what generate wrote (--protect).
        protect = (
            str(policy_file),
            *policy.control_paths(policy_file.parent),
            *(str(Path(p).expanduser().absolute()) for p in args.protect or []),
        )
        reason = check(args.runner, sys.stdin.read(), policy.load(policy_file), protect)
    except BaseException as e:  # noqa: BLE001 - fail closed on anything, including a broken policy file
        reason = f"could not check this call ({type(e).__name__}: {e}); blocked to be safe"
    if reason is None:
        return 0
    print(f"ws gate: blocked: {reason}", file=sys.stderr)
    return DENY


def write_files(out: Path, files: dict[str, str]) -> list[str]:
    """Write each file only if its text would change. Returns the names written."""
    changed = []
    for name, text in files.items():
        dest = out / name
        if dest.is_file() and dest.read_text(encoding="utf-8") == text:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)
        changed.append(name)
    return changed


def _runner_homes() -> list[Path]:
    home = Path.home()
    return [home / ".claude", home / ".config" / "opencode"]


def cmd_generate(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve()
    for h in _runner_homes():
        if out == h.resolve() or h.resolve() in out.parents:
            print(f"ws: {out} is a runner's own config folder; generate into a separate folder", file=sys.stderr)
            return 1
    policy_file = _policy_arg(args) or policy.policy_path()
    try:
        pol = policy.load(policy_file)
        adapter = adapters.get(args.runner)
        files = adapter.generate(pol, shlex.split(args.ws_cmd), out, policy_file.resolve())
    except (policy.PolicyError, KeyError, ValueError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    changed = write_files(out, files)
    print(f"ws: {len(changed)} changes to {out} ({', '.join(changed)})" if changed else f"ws: 0 changes to {out}")
    print(adapter.launch_hint(str(out)))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = _policy_arg(args) or policy.policy_path()
    if path.is_file():
        try:
            policy.load(path)
        except policy.PolicyError as e:
            print(f"ws: {e}", file=sys.stderr)
            return 1
        print(f"ws: 0 changes to {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy.default_toml(), encoding="utf-8")
    print(f"ws: wrote {path}")
    print("next: `ws gate generate --runner claude-code|opencode --out DIR`")
    return 0


def register(sub) -> None:
    g = sub.add_parser("gate", help="the policy, and the deny rules generated from it for each runner")
    gs = g.add_subparsers(dest="gate_command", required=True)
    names = list(adapters.all_adapters())

    def common(p: argparse.ArgumentParser, runner: bool = True) -> None:
        p.add_argument("--policy", metavar="FILE", help="policy file (default $WS_HOME/policy.toml)")
        if runner:
            p.add_argument("--runner", required=True, choices=names)

    i = gs.add_parser("init", help="write the commented default policy if there is none")
    common(i, runner=False)
    i.set_defaults(func=cmd_init)

    gen = gs.add_parser("generate", help="write a runner's native deny rules and hook into a folder")
    common(gen)
    gen.add_argument("--out", required=True, metavar="DIR", help="folder to write (never a runner's own config)")
    gen.add_argument("--ws-cmd", default="ws", metavar="CMD", help="how the hook runs ws (default: ws)")
    gen.set_defaults(func=cmd_generate)

    c = gs.add_parser("check", help="hook entry: payload on stdin; exit 0 allow, 2 deny")
    common(c)
    c.add_argument(
        "--protect", action="append", metavar="PATH", help="a file or folder of ws's own that a session may not write"
    )
    c.set_defaults(func=cmd_check)
