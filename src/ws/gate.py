"""`ws gate`: turn the policy into each runner's own config, and answer the hooks.

    ws gate init                                   write the commented default policy if there is none
    ws gate generate --runner R --out DIR          write R's native config into DIR (0 changes when it is current)
    ws gate check --runner R                       hook payload on stdin; exit 0 = allow, 2 = deny (reason on stderr)
    ws gate event --runner R                       hook payload on stdin for the events that are not a call's check
    ws gate replay FILE --runner R                 run recorded payloads against the policy; print allow, deny or ask
    ws gate launch --runner R -- ARGV              start a runner inside the sandbox wall (ws.sandbox)

`check` fails closed: a payload it cannot read, a policy that does not load, or any crash is a deny. Exit 4 is not a
deny: inside the OpenCode wall a granted approval cannot be consumed by the hook, so it prints {"claim": ID} and the shim
claims it from the launcher over an inherited socket (see ws.approvals).

The guards the policy turns on (ws.guards) run beside the rules: before a call in `check` (a deny stops it, an
approvable deny asks for a person's approval as `ask_commands` does, a warning goes to stderr with exit 0), and in
`event` for the rest. `event` is advice that leaves the session running: after a tool call a deny or warning is the
reason on stderr with exit 2 (the runner shows it to the model; the call already ran), at session start and on a prompt
a finding is text on stdout (context for the session), on stop it is a stderr line, and any crash is one stderr line with
exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

from ws import adapters, approvals, guards, output, policy, sandbox
from ws.adapters import Call
from ws.output import WsError
from ws.profile import ws_home

DENY = 2
CLAIM = 4
RESULTS = ("allow", "deny", "ask")


class Ask(str):
    """A reason that is a request for a person's approval, not a refusal. It is text like any other reason (a deny to
    the runner); `replay` tells the two apart by the type."""


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
    if call.kind in ("agent", "skill", "message"):
        return None  # the policy has no rule for these; only guards judge them
    raise ValueError(f"unknown call kind {call.kind!r}")


def _calls(adapter, payload: dict) -> list[Call]:
    calls = adapter.parse(payload)
    return [] if calls is None else [calls] if isinstance(calls, Call) else calls


def _context(runner: str, event: str, payload: dict, clock=None, home: Path | None = None) -> guards.Context:
    return guards.Context(
        runner, event, _text(payload, "session_id"), _text(payload, "cwd"), payload, clock or time.time, home
    )


def check(
    runner: str,
    raw: str,
    pol: policy.Policy,
    protect: tuple[str, ...] = (),
    approval_root: Path | None = None,
    consume: bool = True,
    *,
    notes: list[str] | None = None,
    clock=None,
    home: Path | None = None,
) -> str | approvals.Claim | None:
    """None to allow, else the reason. Raises on anything it cannot judge; cmd_check turns that into a deny.
    A shell command that matches `ask_commands` (and nothing denies) runs only on a granted approval bound to this
    runner, command, folder and session; `approval_root` is where those live (None: it cannot ask, so it denies, and
    the reason is an `Ask`). With consume=False (inside the wall) a valid grant comes back as a Claim to be claimed
    from the launcher. The guards that are on judge the call after the rules: the first deny wins, an approvable one
    asks for approval the same way, and warnings and notes are added to `notes`. `home` and `clock` are for the guards
    (the ws home their state lives in, and the time)."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("payload is not a JSON object")
    adapter = adapters.get(runner)
    # A runner that has the sandbox block (WS_SANDBOXED) is checked for a project that widens it; see the adapter.
    preflight = getattr(adapter, "preflight", None)
    if preflight and (why := preflight(payload)):
        return why
    calls = _calls(adapter, payload)
    for call in calls:
        reason = evaluate(pol, call, protect)
        if reason:
            return reason
    session, cwd = _text(payload, "session_id"), _text(payload, "cwd")
    for name, call, found in guards.run(pol, "pre", calls, _context(runner, "pre", payload, clock, home)):
        reason = f"{name}: {found.reason}"
        if found.action != "deny":
            if notes is not None:
                notes.append(reason)
        elif not found.approvable:
            return reason
        elif approval_root is None:
            return Ask(f"{reason}; a person can approve it, and there is no approvals folder to ask through")
        else:
            bound = approvals.Bound(runner, f"guard:{name}", call.value if call else "", cwd, session)
            asked = approvals.require(approval_root, bound, consume=consume, why_asked=reason)
            if asked is not None:
                return asked
    for call in calls:
        if call.kind == "command" and policy.needs_approval(pol, call.value):
            if approval_root is None:
                return Ask("this command needs a person's approval, and there is no approvals folder to ask through")
            bound = approvals.Bound(runner, call.kind, call.value, cwd, session)
            return approvals.require(approval_root, bound, consume=consume)
    return None


def event(runner: str, raw: str, pol: policy.Policy, *, clock=None, home: Path | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) for a hook payload of an event that is not a call's check. Raises on a payload it
    cannot read; cmd_event turns that into exit 0 and one line."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("payload is not a JSON object")
    adapter = adapters.get(runner)
    name = payload.get("hook_event_name")
    ev = getattr(adapter, "EVENT_NAMES", {}).get(name) if isinstance(name, str) else None
    if ev is None or ev == "pre":
        return 0, "", f"ws gate event: nothing to do for {str(name)[:40]!r}\n"
    calls = _calls(adapter, payload) if ev == "post" else []
    found = guards.run(pol, ev, calls, _context(runner, ev, payload, clock, home))
    lines = [f"ws gate: {n}: {f.reason}" for n, _, f in found]
    if not lines:
        return 0, "", ""
    text = "\n".join(lines) + "\n"
    if ev in ("start", "prompt"):
        return 0, text, ""
    if ev == "post" and any(f.action in ("deny", "warn") for _, _, f in found):
        return DENY, "", text
    return 0, "", text


def _text(payload: dict, key: str) -> str:
    """A string the runner sends beside the call (session id, folder), or "" when it sends none."""
    v = payload.get(key)
    return v if isinstance(v, str) else ""


def _policy_arg(args: argparse.Namespace) -> Path | None:
    return Path(args.policy).expanduser().absolute() if args.policy else None


def _protect(policy_file: Path, extra: list[str] | None = None) -> tuple[str, ...]:
    """ws's own files: the policy it reads, the control files beside it, and what generate wrote (--protect)."""
    return (
        str(policy_file),
        *policy.control_paths(policy_file.parent),
        *policy.control_paths(ws_home()),
        *(str(Path(p).expanduser().absolute()) for p in extra or []),
    )


def cmd_check(args: argparse.Namespace) -> int:
    notes: list[str] = []
    try:
        policy_file = _policy_arg(args) or policy.policy_path()
        reason = check(
            args.runner,
            sys.stdin.read(),
            policy.load(policy_file),
            _protect(policy_file, args.protect),
            approvals.approvals_dir(),  # $WS_HOME, whatever --policy says: ws approve and ws gate launch use it too
            consume=approvals.FD_ENV not in os.environ,
            notes=notes,
        )
    except BaseException as e:  # noqa: BLE001 - fail closed on anything, including a broken policy file
        reason = f"could not check this call ({type(e).__name__}: {e}); blocked to be safe"
    for note in notes:
        print(f"ws gate: {note}", file=sys.stderr)
    if reason is None:
        return 0
    if isinstance(reason, approvals.Claim):
        print(json.dumps({"claim": reason.ident}))
        return CLAIM
    print(f"ws gate: blocked: {reason}", file=sys.stderr)
    return DENY


def cmd_event(args: argparse.Namespace) -> int:
    try:
        policy_file = _policy_arg(args) or policy.policy_path()
        code, out, err = event(args.runner, sys.stdin.read(), policy.load(policy_file))
    except Exception as e:  # noqa: BLE001 - advice only: a crash here never blocks the session
        code, out, err = 0, "", f"ws gate event: skipped ({type(e).__name__}: {e})\n"
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return code


def _replay_line(n: int, line: str) -> tuple[dict, str | None]:
    """(payload, expected result or None) from line `n` of a replay file: a bare payload, or an object with a
    `payload` (as `record-calls` writes it) and, optionally, an `expect` of allow, deny or ask."""
    try:
        obj = json.loads(line)
    except ValueError as e:
        raise WsError("bad_input", f"line {n} is not JSON ({e.__class__.__name__})", "one JSON object per line") from e
    if not isinstance(obj, dict):
        raise WsError("bad_input", f"line {n} is not a JSON object", "one JSON object per line")
    expect = obj.get("expect")
    if expect is not None and expect not in RESULTS:
        raise WsError("bad_input", f"line {n}: expect must be one of {', '.join(RESULTS)}", "fix the line")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    return payload, expect


def replay(runner: str, lines: list[str], pol: policy.Policy, protect: tuple[str, ...] = ()) -> list[dict]:
    """Each non-blank line judged as the live hook would, with no approval used up and no guard state kept: a list of
    {line, result (allow, deny or ask), reason, expect, differs}."""
    results = []
    with tempfile.TemporaryDirectory(prefix="ws-replay-") as scratch:
        for n, text in enumerate(lines, 1):
            if not text.strip():
                continue
            payload, expect = _replay_line(n, text)
            try:
                reason = check(runner, json.dumps(payload), pol, protect, None, home=Path(scratch))
            except Exception as e:  # noqa: BLE001 - what the live hook would do: a call it cannot judge is a deny
                reason = f"could not check this call ({type(e).__name__}: {e})"
            result = "allow" if reason is None else "ask" if isinstance(reason, Ask) else "deny"
            results.append(
                {
                    "line": n,
                    "result": result,
                    "reason": reason if isinstance(reason, str) else None,
                    "expect": expect,
                    "differs": expect is not None and expect != result,
                }
            )
    return results


def cmd_replay(args: argparse.Namespace) -> int:
    policy_file = _policy_arg(args) or policy.policy_path()
    try:
        pol = policy.load(policy_file)
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {policy_file}, or `ws gate init`") from e
    source = Path(args.file).expanduser()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise WsError("not_found", f"cannot read {source} ({type(e).__name__})", "give a file of payloads") from e
    results = replay(args.runner, lines, pol, _protect(policy_file))
    differ = [r for r in results if r["differs"]]
    if args.json:
        output.result(
            {"results": results, "differ": len(differ)}, status=output.ATTENTION if differ else output.OK, kind="gate"
        )
        return 1 if differ else 0
    for r in results:
        tail = f"  {r['reason']}" if r["reason"] else ""
        said = f"  (expected {r['expect']})" if r["differs"] else ""
        print(f"{r['line']:>4}  {r['result']:<5}{said}{tail}")
    counts = ", ".join(f"{sum(r['result'] == k for r in results)} {k}" for k in RESULTS)
    print(f"ws: {len(results)} calls: {counts}" + (f"; {len(differ)} differ from expect" if differ else ""))
    return 1 if differ else 0


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
            raise WsError("refused", f"{out} is a runner's own config folder", "generate into a separate folder")
    policy_file = _policy_arg(args) or policy.policy_path()
    try:
        pol = policy.load(policy_file)
        adapter = adapters.get(args.runner)
        files = adapter.generate(pol, shlex.split(args.ws_cmd), out, policy_file.resolve())
    except (policy.PolicyError, KeyError, ValueError) as e:
        raise WsError("bad_config", str(e), f"fix {policy_file}, or `ws gate init`") from e
    changed = write_files(out, files)
    if args.json:
        output.result({"out": str(out), "changed": changed, "launch": adapter.launch_hint(str(out))})
        return 0
    print(f"ws: {len(changed)} changes to {out} ({', '.join(changed)})" if changed else f"ws: 0 changes to {out}")
    print(adapter.launch_hint(str(out)))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = _policy_arg(args) or policy.policy_path()
    if path.is_file():
        try:
            policy.load(path)
        except policy.PolicyError as e:
            raise WsError("bad_config", str(e), f"fix {path}") from e
        if args.json:
            output.result({"path": str(path), "changed": False})
            return 0
        print(f"ws: 0 changes to {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy.default_toml(), encoding="utf-8")
    if args.json:
        output.result({"path": str(path), "changed": True})
        return 0
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
    c.set_defaults(func=cmd_check, raw=True)  # a hook: the runner reads this command's own stdout, stderr and exit code

    e = gs.add_parser(
        "event", help="hook entry: payload on stdin for the guards' other events (after a call, start, stop)"
    )
    common(e)
    e.set_defaults(func=cmd_event, raw=True)  # a hook: the runner reads this command's own stdout, stderr and exit code

    r = gs.add_parser("replay", help="run recorded hook payloads against the policy and print allow, deny or ask")
    r.add_argument("file", metavar="FILE", help="one JSON object per line: a payload, or {payload, expect}")
    common(r)
    r.set_defaults(func=cmd_replay)

    sandbox.add_launch(gs, names)
