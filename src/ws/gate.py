"""`ws gate`: turn the policy into each runner's own config, and answer the hooks.

    ws gate init                                   write the commented default policy if there is none
    ws gate generate --runner R --out DIR          write R's native config into DIR (0 changes when it is current)
    ws gate check --runner R                       hook payload on stdin; exit 0 = allow, 2 = deny (reason on stderr)
    ws gate event --runner R                       hook payload on stdin for the events that are not a call's check
    ws gate replay FILE... --runner R              run recorded payloads against the policy; print allow, deny or ask
    ws gate replay CORPUS... --runner R --metrics  a labelled corpus: confusion matrix, error rates, latency, a decision diff
    ws gate launch --runner R -- ARGV              start a runner inside the sandbox wall (ws.sandbox)
    ws gate serve                                  the resident gate that answers the hooks (ws.gateserve)

Every generated hook runs the thin client (ws.gateclient), which asks the resident gate and, when that can't answer,
runs `check` or `event` itself: `run_hook` is the one decision path they share. The order is fixed: the preflight, the
policy's rules, the guards that are on (lowest PRIORITY first, see ws.guards), then `ask_commands`; the first refusal
ends it. A hook run that asked a guard or refused a call leaves one line in `logs/gate.jsonl`.

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
import contextlib
import json
import os
import shlex
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ws import adapters, approvals, guards, jsonl, output, paths, policy, sandbox, selfcmd, shellwords

if TYPE_CHECKING:
    from ws import gatecorpus
from ws.adapters import Call
from ws.output import WsError

DENY = 2
CLAIM = 4
RESULTS = ("allow", "deny", "ask")

CASELESS_ENV = "WS_FORCE_CASELESS_PATHS"
if os.environ.get(CASELESS_ENV) == "1":
    # A test knob: run the gate as it runs on a case-insensitive file system (macOS) on any machine, in every process that
    # imports it, the resident gate included. It can only turn caseless matching on, never off.
    policy.CASELESS_PATHS = True


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


def _mark(layer: list[str] | None, kind: str) -> None:
    if layer is not None:
        layer.append(kind)


def check(
    runner: str,
    raw: str,
    pol: policy.Policy,
    protect: tuple[str, ...] = (),
    approval_root: Path | None = None,
    consume: bool = True,
    *,
    notes: list[str] | None = None,
    layer: list[str] | None = None,
    clock=None,
    home: Path | None = None,
    verdicts: list[dict] | None = None,
) -> str | approvals.Claim | None:
    """None to allow, else the reason. Raises on anything it cannot judge; cmd_check turns that into a deny.
    A shell command that matches `ask_commands` (and nothing denies) runs only on a granted approval bound to this
    runner, command, folder and session; `approval_root` is where those live (None: it cannot ask, so it denies, and
    the reason is an `Ask`). With consume=False (inside the wall) a valid grant comes back as a Claim to be claimed
    from the launcher. The guards that are on judge the call after the rules: the first deny wins, an approvable one
    asks for approval the same way, and warnings and notes are added to `notes`. `layer`, when given, gets the kind of
    the layer that made a refusal (preflight, rule:KIND, guard:NAME, ask_commands). `home` and `clock` are for the
    guards (the ws home their state lives in, and the time). The rules, the guards and the approval check read the
    command under one clock (see shellwords.time_limit), so a command cannot take them one after another. The order is
    fixed: the preflight, then the policy's rules, then the guards that are on (lowest PRIORITY first, see ws.guards),
    then `ask_commands`; the first refusal ends it and nothing after it is asked. `verdicts`, when given, gets one
    {guard, result, ms} for each guard that was asked."""
    with shellwords.time_limit():
        return _check(runner, raw, pol, protect, approval_root, consume, notes, layer, clock, home, verdicts)


def _check(
    runner: str,
    raw: str,
    pol: policy.Policy,
    protect: tuple[str, ...],
    approval_root: Path | None,
    consume: bool,
    notes: list[str] | None,
    layer: list[str] | None,
    clock,
    home: Path | None,
    verdicts: list[dict] | None,
) -> str | approvals.Claim | None:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("payload is not a JSON object")
    adapter = adapters.get(runner)
    # A session with the sandbox block is checked for a project that widens it (WS_SANDBOXED=0 marks settings
    # written without it); see the adapter.
    preflight = getattr(adapter, "preflight", None)
    if preflight and (why := preflight(payload)):
        _mark(layer, "preflight")
        return why
    calls = _calls(adapter, payload)
    for call in calls:
        reason = evaluate(pol, call, protect)
        if reason:
            _mark(layer, f"rule:{call.kind}")
            return reason
    session, cwd = _text(payload, "session_id"), _text(payload, "cwd")
    stream = guards.stream(pol, "pre", calls, _context(runner, "pre", payload, clock, home), verdicts)
    try:
        for name, call, found in stream:
            reason = f"{name}: {found.reason}"
            if found.action != "deny":
                if notes is not None:
                    notes.append(reason)
            elif not found.approvable:
                _mark(layer, f"guard:{name}")
                return reason
            elif approval_root is None:
                _mark(layer, f"guard:{name}")
                return Ask(
                    f"{reason}; a person approves it from a gated session, where the approvals folder is reachable"
                )
            else:
                bound = approvals.Bound(runner, f"guard:{name}", call.value if call else "", cwd, session)
                asked = approvals.require(approval_root, bound, consume=consume, why_asked=reason)
                if asked is not None:
                    _mark(layer, f"guard:{name}")
                    return asked
    finally:
        stream.close()  # the guards after a refusal are not asked, and the one that refused is timed
    for call in calls:
        if call.kind == "command" and policy.needs_approval(pol, call.value):
            if approval_root is None:
                _mark(layer, "ask_commands")
                return Ask("this command needs a person's approval, given from a gated session (`ws approve`)")
            bound = approvals.Bound(runner, call.kind, call.value, cwd, session)
            asked = approvals.require(approval_root, bound, consume=consume)
            if asked is not None:
                _mark(layer, "ask_commands")
            return asked
    return None


def event(
    runner: str,
    raw: str,
    pol: policy.Policy,
    *,
    clock=None,
    home: Path | None = None,
    verdicts: list[dict] | None = None,
) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) for a hook payload of an event that is not a call's check. Raises on a payload it
    cannot read; cmd_event turns that into exit 0 and one line. `verdicts` is as for `check`."""
    with shellwords.time_limit():
        return _event(runner, raw, pol, clock, home, verdicts)


def _event(
    runner: str, raw: str, pol: policy.Policy, clock, home: Path | None, verdicts: list[dict] | None
) -> tuple[int, str, str]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("payload is not a JSON object")
    adapter = adapters.get(runner)
    name = payload.get("hook_event_name")
    ev = getattr(adapter, "EVENT_NAMES", {}).get(name) if isinstance(name, str) else None
    if ev is None or ev == "pre":
        return (
            0,
            "",
            f"ws gate event: {str(name)[:40]!r} is answered by `ws gate check` or is not a hook event ws reads\n",
        )
    calls = _calls(adapter, payload) if ev == "post" else []
    found = guards.run(pol, ev, calls, _context(runner, ev, payload, clock, home), verdicts)
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


def _load_policy(args: argparse.Namespace) -> tuple[Path, policy.Policy]:
    """The policy file and the policy in it. A file that was named (`--policy`, which every generated hook carries) must
    be there and load: if it is missing or unreadable that is an error (`policy_missing`), never the default policy.
    The default applies only when no file was named at all."""
    named = _policy_arg(args)
    policy_file = named or policy.policy_path()
    try:
        return policy_file, policy.load(policy_file, required=named is not None)
    except policy.PolicyError as e:
        raise WsError(
            "policy_missing" if named is not None and not policy_file.is_file() else "bad_config",
            str(e),
            "run `ws install` to write the policy and the hooks again",
        ) from e


def _pinned(args: argparse.Namespace):
    """A hook runs on the layout `ws install` pinned in its command, and on no other: the environment is the session's
    (a project's settings can set WS_HOME or an XDG variable), so it must resolve to the same folders or the call is
    refused. Inside the block the whole process answers with the pinned layout (see ws.paths.pinned_by). A hook with no
    pin at all (an earlier ws wrote it) is not refused: it runs the full check, and the block yields the note that names
    `ws upgrade`."""
    return paths.pinned_by(args.pin, allow_unpinned=True)


def _reason_of(e: BaseException) -> str:
    if isinstance(e, WsError):
        return f"{e.message}; {e.hint}" if e.hint else e.message
    return f"could not check this call ({type(e).__name__}: {e}); blocked to be safe"


def _protect(policy_file: Path, extra: list[str] | None = None) -> tuple[str, ...]:
    """ws's own files: the policy it reads, the control files beside it, every folder ws keeps its files in (in each layout
    it could use) and what generate wrote (--protect)."""
    return (
        str(policy_file),
        *paths.control_paths_in(policy_file.parent),
        *paths.control_paths(),
        *map(str, paths.protected_roots()),
        *(str(Path(p).expanduser().absolute()) for p in extra or []),
    )


VERDICT_LOG = "gate.jsonl"
VERDICT_SCHEMA = 1


def _log_verdicts(rec: dict) -> None:
    """One line in `<state>/logs/gate.jsonl` for a hook run that asked a guard or refused something: what was decided,
    by which layer, and each guard's verdict. It holds no command, path or message text. A log that can't be written
    never changes the decision."""
    try:
        jsonl.append(paths.state_dir() / "logs" / VERDICT_LOG, jsonl.stamped(rec, VERDICT_SCHEMA))
    except (OSError, ValueError):
        pass


def _session_of(raw: str) -> tuple[str, str]:
    """(session id, hook event name) from a payload, "" for either that is missing or not text. Never raises."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return _text(payload, "session_id")[:80], _text(payload, "hook_event_name")[:40]


def _decision(reason: str | approvals.Claim | None) -> str:
    if reason is None:
        return "allow"
    if isinstance(reason, approvals.Claim):
        return "claim"
    return "ask" if isinstance(reason, Ask) else "deny"


def _warning_out(runner: str, note: str) -> str:
    """What an allowed call prints on stdout so the person sees `note`. Claude Code does not show the stderr of a hook that
    exits 0, but it shows the `systemMessage` of one JSON object on stdout, and that changes nothing about the call.
    Other runners read the exit code and stderr only, so they get nothing here."""
    return json.dumps({"systemMessage": f"ws: {note}"}) + "\n" if runner == "claude-code" else ""


def run_check(args: argparse.Namespace, raw: str, via: str = "direct") -> tuple[int, str, str]:
    """What `ws gate check` does for one payload, as (exit code, stdout, stderr): 0 allow, 2 deny (the reason on
    stderr), 4 a claim for the launcher. Fails closed: any error is a deny. The command and the daemon both call this,
    so they cannot answer differently. `via` is "direct" or "daemon" in the verdict log."""
    notes: list[str] = []
    layer: list[str] = []
    verdicts: list[dict] = []
    unpinned = False
    began = time.perf_counter()
    try:
        with _pinned(args) as warned:
            unpinned = bool(warned)
            notes.extend(warned)
            policy_file, pol = _load_policy(args)
            try:
                reason = check(
                    args.runner,
                    raw,
                    pol,
                    _protect(policy_file, args.protect),
                    approvals.approvals_dir(),  # ws's state folder, whatever --policy says: ws approve and ws gate launch use it too
                    consume=approvals.FD_ENV not in os.environ,
                    notes=notes,
                    layer=layer,
                    verdicts=verdicts,
                )
            except BaseException as e:  # noqa: BLE001 - fail closed on anything
                reason, layer = _reason_of(e), ["error"]
            if verdicts or reason is not None:
                session, name = _session_of(raw)
                _log_verdicts(
                    {
                        "runner": args.runner,
                        "session": session,
                        "event": name or "PreToolUse",
                        "decision": _decision(reason),
                        "layer": layer[0] if layer else None,
                        "guards": verdicts,
                        "ms": round((time.perf_counter() - began) * 1000, 2),
                        "via": via,
                    }
                )
    except BaseException as e:  # noqa: BLE001 - fail closed on anything, including a broken policy file
        reason = _reason_of(e)
    err = "".join(f"ws gate: {note}\n" for note in notes)
    if reason is None:
        return 0, _warning_out(args.runner, paths.UNPINNED_NOTE) if unpinned else "", err
    if isinstance(reason, approvals.Claim):
        return CLAIM, json.dumps({"claim": reason.ident}) + "\n", err
    return DENY, "", err + f"ws gate: blocked: {reason}\n"


def run_event(args: argparse.Namespace, raw: str, via: str = "direct") -> tuple[int, str, str]:
    """What `ws gate event` does for one payload, as (exit code, stdout, stderr). Advice only: any failure is exit 0
    and one line on stderr."""
    verdicts: list[dict] = []
    began = time.perf_counter()
    try:
        with _pinned(args) as warned:
            _, pol = _load_policy(args)
            code, out, err = event(args.runner, raw, pol, verdicts=verdicts)
            err = "".join(f"ws gate event: {note}\n" for note in warned) + err
            if verdicts:
                session, name = _session_of(raw)
                _log_verdicts(
                    {
                        "runner": args.runner,
                        "session": session,
                        "event": name,
                        "decision": "deny" if code == DENY else "allow",
                        "layer": None,
                        "guards": verdicts,
                        "ms": round((time.perf_counter() - began) * 1000, 2),
                        "via": via,
                    }
                )
    except WsError as e:  # advice only: ws's folders or its policy are not as installed, so the guards do not run
        code, out, err = 0, "", f"ws gate event: skipped ({_reason_of(e)})\n"
    except Exception as e:  # noqa: BLE001 - advice only: a crash here never blocks the session
        code, out, err = 0, "", f"ws gate event: skipped ({type(e).__name__}: {e})\n"
    return code, out, err


def _hook_options(p: argparse.ArgumentParser, names: list[str], protect: bool) -> None:
    """The options `ws gate check` and `ws gate event` take: one definition for the command and for the daemon."""
    p.add_argument("--policy", metavar="FILE", help="policy file (default: policy.toml in the config folder)")
    p.add_argument("--runner", required=True, choices=names)
    paths.add_pin_option(p)
    if protect:
        p.add_argument(
            "--protect",
            action="append",
            metavar="PATH",
            help="a file or folder of ws's own that a session may not write",
        )


def hook_args(kind: str, argv: list[str]) -> argparse.Namespace:
    """The options of `ws gate check` or `ws gate event` read from `argv` (what follows the subcommand), by the same
    definitions the command uses. A bad option raises SystemExit, as it does on the command line."""
    parser = argparse.ArgumentParser(prog=f"ws gate {kind}", add_help=False)
    _hook_options(parser, list(adapters.all_adapters()), protect=kind == "check")
    return parser.parse_args(argv)


def run_hook(kind: str, argv: list[str], raw: str, via: str = "direct") -> tuple[int, str, str]:
    """`ws gate check` (kind "check") or `ws gate event` (kind "event") with these options on this payload, as (exit
    code, stdout, stderr): the one decision path the command line and the daemon share (see ws.gateserve)."""
    if kind not in ("check", "event"):
        raise ValueError(f"unknown hook kind {kind!r}")
    args = hook_args(kind, argv)
    return (run_check if kind == "check" else run_event)(args, raw, via)


def _emit(result: tuple[int, str, str]) -> int:
    code, out, err = result
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return code


def _stdin_text() -> str:
    """The payload on stdin. Bytes that aren't text give a payload no JSON reader takes, so `check` denies it and
    `event` skips it, as for any payload it can't read."""
    try:
        return sys.stdin.read()
    except UnicodeDecodeError:
        return "\0 not text"


def cmd_check(args: argparse.Namespace) -> int:
    return _emit(run_check(args, _stdin_text()))


def cmd_event(args: argparse.Namespace) -> int:
    return _emit(run_event(args, _stdin_text()))


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


def _judge(runner: str, payload: dict, pol: policy.Policy, protect: tuple[str, ...], scratch: Path) -> dict:
    """One call judged as the live hook would, with no approval used up and no guard state kept: {result (allow, deny
    or ask), reason, kind (the layer that refused it, None for an allow), ms (the time check() took)}."""
    layer: list[str] = []
    began = time.perf_counter()
    try:
        reason = check(runner, json.dumps(payload), pol, protect, None, home=scratch, layer=layer)
    except Exception as e:  # noqa: BLE001 - what the live hook would do: a call it cannot judge is a deny
        reason = f"could not check this call ({type(e).__name__}: {e})"
        layer = ["error"]
    ms = (time.perf_counter() - began) * 1000
    result = "allow" if reason is None else "ask" if isinstance(reason, Ask) else "deny"
    return {
        "result": result,
        "reason": reason if isinstance(reason, str) else None,
        "kind": (layer[0] if layer else "unknown") if reason is not None else None,
        "ms": ms,
    }


def replay(runner: str, lines: list[str], pol: policy.Policy, protect: tuple[str, ...] = ()) -> list[dict]:
    """Each non-blank line judged as the live hook would, with no approval used up and no guard state kept: a list of
    {line, result (allow, deny or ask), reason, expect, differs}."""
    results = []
    with tempfile.TemporaryDirectory(prefix="ws-replay-") as scratch:
        for n, text in enumerate(lines, 1):
            if not text.strip():
                continue
            payload, expect = _replay_line(n, text)
            judged = _judge(runner, payload, pol, protect, Path(scratch))
            results.append(
                {
                    "line": n,
                    "result": judged["result"],
                    "reason": judged["reason"],
                    "expect": expect,
                    "differs": expect is not None and expect != judged["result"],
                }
            )
    return results


@contextlib.contextmanager
def _scratch_home(home: Path):
    """HOME, USERPROFILE and WS_HOME (one folder, `home/.ws`) point into `home` for the block, so `~` and `$HOME` in a corpus name the scratch
    folder and not the person's own."""
    keys = ("HOME", "USERPROFILE", "WS_HOME")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(
        {"HOME": str(home), "USERPROFILE": str(home), "WS_HOME": str(paths.legacy_dir({"HOME": str(home)}))}
    )
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def replay_corpus(runner: str, rows: list[gatecorpus.Row], policy_file: Path | None = None) -> list[dict]:
    """Each corpus row judged as the live hook would, in scratch `HOME` and `CWD` folders (made here, removed after,
    and the only place a `{HOME}` or `{CWD}` is filled with). The policy is `policy_file`, else the built-in one. A row
    that switches on guards gets a folder of its own for the state they keep, so a decision does not depend on which
    rows came before it. A row for another runner is left out. Each result is {id, source, expect, known, result,
    reason, kind, ms, differs}."""
    from ws import gatecorpus  # here, not at the top: the live hook doesn't pay for the corpus code

    results = []
    with tempfile.TemporaryDirectory(prefix="ws-corpus-") as scratch:
        home, cwd = Path(scratch) / "home", Path(scratch) / "cwd"
        home.mkdir()
        cwd.mkdir()
        with _scratch_home(home):
            try:
                pol = policy.load(policy_file, required=True) if policy_file else policy.default_policy()
            except policy.PolicyError as e:
                raise WsError("bad_config", str(e), f"fix {policy_file}, or write the default policy") from e
            protect = _protect(policy_file or paths.config_dir() / "policy.toml")
            for n, row in enumerate(rows):
                payload = gatecorpus.render(row, runner, str(home), str(cwd))
                if payload is None:
                    continue
                on = tuple(dict.fromkeys((*pol.guards.on, *row.guards)))
                state = home
                if row.guards:  # a guard that keeps state starts every row it is switched on for from nothing
                    state = Path(scratch) / f"guard-state-{n}"
                    state.mkdir()
                judged = _judge(runner, payload, replace(pol, guards=replace(pol.guards, on=on)), protect, state)
                results.append(
                    {
                        "id": row.id,
                        "source": row.source,
                        "expect": row.expect,
                        "known": row.known,
                        **judged,
                        "differs": row.expect != judged["result"],
                    }
                )
    return results


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise WsError("not_found", f"cannot read {path} ({type(e).__name__})", "give a file of payloads") from e


def cmd_replay(args: argparse.Namespace) -> int:
    from ws import gatecorpus

    files = [Path(f).expanduser() for f in args.file]
    if args.expect_changes and not args.diff:
        raise WsError("bad_input", "--expect-changes needs --diff", "give the saved decisions to compare with", 2)
    corpus = (
        args.metrics or args.save or args.diff or any(gatecorpus.is_corpus("\n".join(_read_lines(f))) for f in files)
    )
    if corpus:
        return _replay_corpus_cmd(args, files)
    if len(files) != 1:
        raise WsError("bad_input", "give one file of payloads", "several files are for a corpus (rows with an id)")
    policy_file = _policy_arg(args) or policy.policy_path()
    try:
        pol = policy.load(policy_file, required=args.policy is not None)
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {policy_file}, or `ws gate init`") from e
    results = replay(args.runner, _read_lines(files[0]), pol, _protect(policy_file))
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


def _replay_corpus_cmd(args: argparse.Namespace, files: list[Path]) -> int:
    from ws import gatecorpus

    results = replay_corpus(args.runner, gatecorpus.load(files), _policy_arg(args))
    bad, ratchet = gatecorpus.failures(results)
    data: dict = {
        "runner": args.runner,
        "rows": len(results),
        "differ": [r["id"] for r in bad],
        "now_pass": [r["id"] for r in ratchet],
    }
    if args.metrics:
        data["metrics"] = gatecorpus.metrics(results)
    changes, unlisted, stale = None, [], []
    if args.diff:
        changes = gatecorpus.diff(gatecorpus.read_snapshot(Path(args.diff).expanduser(), args.runner), results)
        expected = gatecorpus.read_expected(Path(args.expect_changes).expanduser()) if args.expect_changes else {}
        unlisted, stale = gatecorpus.unexpected(changes, expected)
        data["diff"] = {**changes, "unexpected": [c["id"] for c in unlisted], "expected_but_unchanged": stale}
    if args.save:
        out = Path(args.save).expanduser()
        try:
            out.write_text(json.dumps(gatecorpus.snapshot(args.runner, results), indent=1, sort_keys=True) + "\n")
        except OSError as e:
            raise WsError(
                "io", f"cannot write {out} ({type(e).__name__})", "give a file in a folder that exists"
            ) from e
        data["saved"] = str(out)
    failed = bool(bad or ratchet or unlisted or stale)
    if args.json:
        data["results"] = results
        output.result(data, status=output.ATTENTION if failed else output.OK, kind="gate")
        return 1 if failed else 0
    for r in bad:
        tail = f"  {r['reason']}" if r["reason"] else ""
        print(f"FAIL  {r['id']}: expected {r['expect']}, got {r['result']} ({r['source']}){tail}")
    for r in ratchet:
        print(f'FIXED {r["id"]}: now {r["result"]} as expected; remove its "known" mark')
    if args.metrics:
        print("\n".join(gatecorpus.report_lines(data["metrics"])))
    if changes is not None:
        print()
        for c in changes["changed"]:
            note = "  (not listed in --expect-changes)" if c in unlisted else ""
            print(f"changed {c['id']}: {gatecorpus.change_text(c)}  [{c['from']['kind']} -> {c['to']['kind']}]{note}")
        for i in stale:
            print(f"listed but unchanged: {i}")
        if changes["added"] or changes["removed"]:
            print(f"rows added {len(changes['added'])}, removed {len(changes['removed'])} since the saved decisions")
        print(f"{len(changes['changed'])} changed, {len(unlisted)} not listed")
    counts = ", ".join(f"{sum(r['result'] == k for r in results)} {k}" for k in RESULTS)
    print(f"ws: {len(results)} rows: {counts}; {len(bad)} differ, {len(ratchet)} known rows now pass")
    return 1 if failed else 0


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
        pol = policy.load(policy_file, required=args.policy is not None)
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
        p.add_argument("--policy", metavar="FILE", help="policy file (default: policy.toml in the config folder)")
        if runner:
            p.add_argument("--runner", required=True, choices=names)

    i = gs.add_parser("init", help="write the commented default policy if there is none")
    common(i, runner=False)
    i.set_defaults(func=cmd_init)

    gen = gs.add_parser("generate", help="write a runner's native deny rules and hook into a folder")
    common(gen)
    gen.add_argument("--out", required=True, metavar="DIR", help="folder to write (never a runner's own config)")
    gen.add_argument(
        "--ws-cmd",
        default=shlex.join(selfcmd.argv()),
        metavar="CMD",
        help="how the hook runs ws (default: this Python, isolated, -m ws)",
    )
    gen.set_defaults(func=cmd_generate)

    c = gs.add_parser("check", help="hook entry: payload on stdin; exit 0 allow, 2 deny")
    _hook_options(c, names, protect=True)
    c.set_defaults(func=cmd_check, raw=True)  # a hook: the runner reads this command's own stdout, stderr and exit code

    e = gs.add_parser(
        "event", help="hook entry: payload on stdin for the guards' other events (after a call, start, stop)"
    )
    _hook_options(e, names, protect=False)
    e.set_defaults(func=cmd_event, raw=True)  # a hook: the runner reads this command's own stdout, stderr and exit code

    from ws import gateserve  # here, not at the top: it imports this module

    gateserve.register(gs)

    r = gs.add_parser("replay", help="run recorded hook payloads against the policy and print allow, deny or ask")
    r.add_argument(
        "file",
        nargs="+",
        metavar="FILE",
        help="one JSON object per line: a payload, or {payload, expect}; or labelled corpus files (rows with an id)",
    )
    common(r)
    r.add_argument("--metrics", action="store_true", help="a corpus: the confusion matrix, error rates and latency")
    r.add_argument("--save", metavar="OUT.json", help="a corpus: write each row's decision and layer to OUT.json")
    r.add_argument(
        "--diff", metavar="OLD.json", help="a corpus: list each decision that changed since OLD.json was saved"
    )
    r.add_argument(
        "--expect-changes",
        metavar="FILE",
        help="with --diff: the ids expected to change, one a line (ID or ID: OLD -> NEW)",
    )
    r.set_defaults(func=cmd_replay)

    sandbox.add_launch(gs, names)
