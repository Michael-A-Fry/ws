"""Replay a list of shell commands or recorded hook payloads through the gate, by the two ways a hook is answered, and
say whether anything changed against an earlier run.

    python3 scripts/replay_real.py CALLS.json [--baseline FILE] [--out FILE] [--src DIR] [--policy FILE] [--timing N]

CALLS.json is a JSON list. Each item is a shell command (a string, run as a Bash call), or a hook payload as a runner
sends it (`{"tool_name", "tool_input", ...}`), or one line of the `record-calls` log (`{"payload": {...}}`).

Each call is decided twice:

  dispatcher   the generated hook's own command: the thin client (ws.gateclient) asking a resident `ws gate serve`
               that this script starts in a folder of its own, and stops at the end
  in-process   gate.run_hook in this process: what `ws gate check` runs when there is no daemon

`--baseline FILE` is the `--out` of an earlier run (of this script against another build: point `--src` at that build's
`src` folder). The result is compared call by call, and the last lines say how many calls are newly allowed (the
baseline refused or asked, now allowed) and newly refused (the baseline allowed, now refused or asked). Exit 0 only
when both are 0 and the two ways agree on every call. A build with no resident gate (before ws.gateclient) is replayed
in-process only, which is what makes its `--out` a baseline.

Nothing is printed about a call except its position in the list, so a list of real commands stays private. The files
`--out` writes hold a hash and a decision for each call, no command text. Decisions are made in a scratch WS_HOME;
HOME is left as it is, so `~` in a command means what it means to you. `--timing N` also times N calls through the
client with a daemon and with `WS_GATE_DAEMON=0`, and prints p50 and p95.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "claude-code"
DECISION = {0: "allow", 2: "deny", 4: "ask"}  # the hook exit codes: 4 is a call that needs a person's yes
WAIT = 30.0  # seconds to wait for the daemon's socket


def payload_of(item: object, cwd: str) -> dict | None:
    """The hook payload for one item of the list, or None when it is none of the three shapes."""
    if isinstance(item, str):
        return {"tool_name": "Bash", "tool_input": {"command": item}, "session_id": "replay", "cwd": cwd}
    if not isinstance(item, dict):
        return None
    inner = item.get("payload") if isinstance(item.get("payload"), dict) else item
    if "tool_name" in inner:
        return {"session_id": "replay", "cwd": cwd, **inner}
    if isinstance(inner.get("command"), str):
        return {"tool_name": "Bash", "tool_input": {"command": inner["command"]}, "session_id": "replay", "cwd": cwd}
    return None


def key_of(item: object) -> str:
    """A short hash of the item: how a baseline names a call without keeping it."""
    text = json.dumps(item, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


class Replay:
    """The two ways a hook is answered, each with a scratch WS_HOME of its own so that a guard's state (what a session
    has loaded, how many agents it started) kept by one way never reaches the other."""

    def __init__(self, src: Path, policy: Path | None, scratch: Path) -> None:
        self.src, self.policy, self.scratch = src, policy, scratch
        os.environ.pop("WS_GATE_DAEMON", None)
        sys.path.insert(0, str(src))
        from ws import gate, paths

        self.gate, self.paths = gate, paths
        try:
            from ws import adapters, gateclient

            self.client = gateclient
            self.adapters = adapters
        except ImportError:
            self.client = self.adapters = None
        self.ws_cmd = [sys.executable, "-m", "ws"]  # not -I: the --src folder reaches the child by PYTHONPATH
        self.worlds = {name: self.world(name) for name in ("inprocess", "dispatcher")}
        self.daemon: subprocess.Popen | None = None

    def world(self, name: str) -> dict:
        home = self.scratch / name
        home.mkdir()
        os.environ["WS_HOME"] = str(home)
        pin = self.paths.pin_of()
        options = ["--runner", RUNNER, *pin.args()]
        if self.policy is not None:
            options += ["--policy", str(self.policy)]
        return {"home": str(home), "pin": pin, "options": options, "env": {**os.environ, "PYTHONPATH": str(self.src)}}

    # --- in-process ---------------------------------------------------------------------------------------------

    def inprocess(self, raw: str) -> str:
        """The decision in this process. A build before `run_hook` is asked through its own `gate check` command, run here
        with the payload on a replaced stdin."""
        w = self.worlds["inprocess"]
        os.environ["WS_HOME"] = w["home"]
        if hasattr(self.gate, "run_hook"):
            code = self.gate.run_hook("check", w["options"], raw)[0]
        else:
            from ws import cli

            args = cli.build_parser().parse_args(["gate", "check", *w["options"]])
            keep = sys.stdin, sys.stdout, sys.stderr
            sys.stdin, sys.stdout, sys.stderr = io.StringIO(raw), io.StringIO(), io.StringIO()
            try:
                code = args.func(args)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
            finally:
                sys.stdin, sys.stdout, sys.stderr = keep
        return DECISION.get(code, f"exit {code}")

    # --- the dispatcher -----------------------------------------------------------------------------------------

    def client_cmd(self) -> list[str]:
        w = self.worlds["dispatcher"]
        command = [*self.ws_cmd, "gate", "check", *w["options"]]
        return self.adapters.client_argv("check", command, w["pin"])

    def start_daemon(self) -> bool:
        """Start a resident gate for the dispatcher's scratch state folder and wait for its socket."""
        w = self.worlds["dispatcher"]
        self.daemon = subprocess.Popen(
            [*self.ws_cmd, "gate", "serve", *w["pin"].args(), "--idle", "3600"],
            env=w["env"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        socket_file = Path(w["pin"].state) / self.client.RUN / self.client.SOCKET
        end = time.monotonic() + WAIT
        while time.monotonic() < end:
            if socket_file.exists():
                return True
            if self.daemon.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def stop_daemon(self) -> None:
        if self.daemon is not None and self.daemon.poll() is None:
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()

    def dispatched(self, raw: str, daemon: bool = True) -> tuple[str, float]:
        """(the decision through the generated hook command, seconds it took)."""
        env = dict(self.worlds["dispatcher"]["env"])
        env["WS_GATE_DAEMON"] = "1" if daemon else "0"
        began = time.perf_counter()
        done = subprocess.run(self.client_cmd(), input=raw.encode(), env=env, capture_output=True, check=False)
        return DECISION.get(done.returncode, f"exit {done.returncode}"), time.perf_counter() - began

    def served(self) -> int:
        """How many verdict lines carry `via` "daemon": proof the dispatcher path reached the daemon."""
        log = Path(self.worlds["dispatcher"]["pin"].state) / "logs" / "gate.jsonl"
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        count = 0
        for line in lines:
            try:
                count += json.loads(line).get("via") == "daemon"
            except ValueError:
                continue
        return count


def load_calls(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("replay_real: the calls file must be a JSON list")
    return data


def load_baseline(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict):
        raise SystemExit("replay_real: the baseline is not a file this script wrote (--out)")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="replay_real.py", description="Replay calls through the dispatcher and in-process."
    )
    ap.add_argument("calls", type=Path, help="JSON list of shell commands or hook payloads")
    ap.add_argument("--baseline", type=Path, help="the --out of an earlier run, to compare with")
    ap.add_argument("--out", type=Path, help="write this run's decisions (hashes and decisions, no commands)")
    ap.add_argument("--src", type=Path, default=ROOT / "src", help="the ws source folder to replay with")
    ap.add_argument("--policy", type=Path, help="a policy file (default: the built-in one)")
    ap.add_argument("--cwd", default="/", help="the folder the calls are said to run in")
    ap.add_argument("--timing", type=int, default=0, metavar="N", help="also time N calls, with and without a daemon")
    args = ap.parse_args(argv)

    items = load_calls(args.calls)
    baseline = load_baseline(args.baseline) if args.baseline else None
    with tempfile.TemporaryDirectory(prefix="replay-real-") as tmp:
        rep = Replay(args.src.resolve(), args.policy.resolve() if args.policy else None, Path(tmp))
        try:
            return run(rep, items, baseline, args)
        finally:
            rep.stop_daemon()


def run(rep: Replay, items: list, baseline: dict[str, str] | None, args: argparse.Namespace) -> int:
    payloads = [(i, payload_of(item, args.cwd)) for i, item in enumerate(items)]
    unreadable = [i for i, p in payloads if p is None]
    inproc: dict[int, str] = {}
    for i, p in payloads:
        if p is not None:
            inproc[i] = rep.inprocess(json.dumps(p))

    dispatch: dict[int, str] = {}
    note = "no resident gate in this build: in-process only"
    if rep.client is not None:
        note = ""
        if not rep.start_daemon():
            print("replay_real: the resident gate did not start", file=sys.stderr)
            return 1
        for i, p in payloads:
            if p is not None:
                dispatch[i] = rep.dispatched(json.dumps(p))[0]
        used = rep.served()
        asked = sum(1 for d in inproc.values() if d != "allow")
        if (
            used < asked
        ):  # a refused call always leaves a verdict line; fewer than refused means the client decided alone
            print(f"replay_real: only {used} of {asked} refusals came from the daemon", file=sys.stderr)
            return 1

    disagree = [i for i in dispatch if dispatch[i] != inproc.get(i)]
    keys = [key_of(item) for item in items]
    if args.out:
        args.out.write_text(
            json.dumps({"version": 1, "results": {keys[i]: d for i, d in inproc.items()}}, indent=1) + "\n",
            encoding="utf-8",
        )
    newly_allowed = newly_refused = missing = 0
    if baseline is not None:
        for i, now in inproc.items():
            before = baseline.get(keys[i])
            if before is None:
                missing += 1
            elif before != "allow" and now == "allow":
                newly_allowed += 1
                print(f"newly allowed: call {i}")
            elif before == "allow" and now != "allow":
                newly_refused += 1
                print(f"newly refused: call {i}")
    counts = {d: list(inproc.values()).count(d) for d in sorted(set(inproc.values()))}
    print(f"calls {len(items)}: {counts or 'none'}; unreadable {len(unreadable)}")
    if note:
        print(note)
    else:
        print(
            f"dispatcher and in-process disagree on {len(disagree)}" + "".join(f"\n  call {i}" for i in disagree[:20])
        )
    if baseline is not None:
        print(f"not in the baseline {missing}")
        print(f"newly allowed {newly_allowed}, newly refused {newly_refused}")
    if args.timing and rep.client is not None:
        timing(rep, [p for _, p in payloads if p is not None], args.timing)
    clean = not disagree and not newly_allowed and not newly_refused and not missing and not unreadable
    return 0 if clean else 1


def timing(rep: Replay, payloads: list[dict], n: int) -> None:
    """p50 and p95 of n calls through the client: with the daemon up (after one warm-up), then with WS_GATE_DAEMON=0."""
    if not payloads:
        return
    chosen = [json.dumps(payloads[i % len(payloads)]) for i in range(n)]
    rep.dispatched(chosen[0])
    for label, daemon in (("daemon", True), ("in-process fallback", False)):
        ms = [rep.dispatched(raw, daemon)[1] * 1000 for raw in chosen]
        print(f"{label}: {n} calls, p50 {percentile(ms, 50):.0f} ms, p95 {percentile(ms, 95):.0f} ms")


if __name__ == "__main__":
    sys.exit(main())
