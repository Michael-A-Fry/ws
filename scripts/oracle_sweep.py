"""The oracle sweep: every row the real-bash oracle knows (cold-review probes, consumers, LIMIT rows, tables, producers,
messages and address forms) through the gate, and each allowed row run in bwrap. A release gate: exit 0 only when no
row the gate allows leaks (LIMIT rows excepted, which are listed limits) and no run errors.

Usage (Linux with bubblewrap): ws-test --light -- uv run --locked python -I scripts/oracle_sweep.py [REPO_ROOT]"""

import os
import shutil
import sys
import time

wt = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [wt + "/tests", wt + "/src"]
os.environ["HOME"] = "/home/pat"
os.environ["WS_HOME"] = "/home/pat/.ws"
for v in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CDPATH", "TMPDIR"):
    os.environ.pop(v, None)
import test_gate_oracle as t

from ws import policy

pol = policy.default_policy()
secs = t.sections()
plain = secs[""]
consumers = secs["consumers"]
limits_file = secs["limits"]
groups = {
    "probes: plain (cold reviews)": [x for x in plain if x not in t.LIMITS],
    "probes: consumers (gate must deny)": consumers,
    "probes: listed LIMIT rows": list(t.LIMITS),
    "tables": t.table_lines(),
    "producers": t.producer_lines(),
    "messages": t.message_lines(),
}
for tool, helps in t.ADDRESS_TOOLS:
    if shutil.which(tool):
        groups[f"address:{tool}"] = t.address_lines(tool, helps)

tot = {"rows": 0, "runs": 0, "allowed": 0, "denied": 0, "leaks": 0, "errors": 0}
t0 = time.monotonic()
for name, lines in groups.items():
    allowed = [x for x in lines if t.gate(pol, x) is None]
    denied = len(lines) - len(allowed)
    if name == "probes: listed LIMIT rows":
        allowed_l = allowed
        bad, errors = t._run_all(allowed_l, 8)
        kinds = {}
        for line in allowed_l:
            if line.startswith(("python3 -c", "awk")):
                k = "interpreter program text"
            elif "/tmp/x.sh" in line:
                k = "script file written then run"
            elif "ProxyCommand" in line:
                k = "option whose value is a command"
            else:
                k = "interpreter reading stdin"
            kinds.setdefault(k, [0, 0])
            kinds[k][0] += 1
            kinds[k][1] += line in bad
        print(f"{name}: rows={len(lines)} allowed={len(allowed)} leaks_seen={len(bad)} errors={len(errors)}")
        for k, (n, seen) in sorted(kinds.items()):
            print(f"   LIMIT kind '{k}': {n} rows, oracle saw a leak in {seen}")
        for e in errors[:5]:
            print("   ERR", e[:200])
        tot["rows"] += len(lines)
        tot["allowed"] += len(allowed)
        tot["errors"] += len(errors)
        continue
    bad, errors = t._run_all(allowed, 8) if allowed else ({}, [])
    print(f"{name}: rows={len(lines)} gate-denied={denied} run={len(allowed)} leaks={len(bad)} errors={len(errors)}")
    for k, v in list(bad.items())[:5]:
        print("   LEAK", repr(k[:100]), v)
    for e in errors[:5]:
        print("   ERR", e[:200])
    tot["rows"] += len(lines)
    tot["allowed"] += len(allowed)
    tot["denied"] += denied
    tot["leaks"] += len(bad)
    tot["errors"] += len(errors)
print("TOTAL", tot, f"{time.monotonic() - t0:.0f}s")
sys.exit(1 if tot["leaks"] or tot["errors"] else 0)
