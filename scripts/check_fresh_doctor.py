"""CI pass test for phase 2: on a fresh machine `ws doctor --json` (the ws.cli/1 envelope) exits non-zero and lists every role unconfigured.

usage: check_fresh_doctor.py REPORT.json EXIT_CODE [--native-windows]
With --native-windows the platform check must fail with the WSL2 fix instead (the core runs in WSL2 on Windows).
"""

import json
import os
import sys
from pathlib import Path

from ws.doctor import ROLES


def main(argv: list[str]) -> int:
    envelope, code = json.loads(Path(argv[0]).read_text(encoding="utf-8")), int(argv[1])
    native_windows = "--native-windows" in argv[2:]
    problems = []
    if envelope.get("schema") != "ws.cli/1" or envelope.get("kind") != "doctor":
        problems.append(f"not a doctor envelope: schema {envelope.get('schema')!r}, kind {envelope.get('kind')!r}")
    if envelope.get("status") != "attention":
        problems.append(f"envelope status {envelope.get('status')!r}, expected 'attention'")
    report = envelope.get("data") or {}
    checks = {c["id"]: c for c in report.get("checks", [])}
    if code != 1:
        problems.append(f"exit code {code}, expected 1")
    if report.get("ok") is not False:
        problems.append("report ok is not false")
    for r in ROLES:
        c = checks.get(r.id)
        if not c or c["status"] != "unconfigured" or c["role"] != r.name:
            problems.append(f"{r.id} {r.name}: {c and c['status']}, expected unconfigured")
    platform = checks.get("WS001", {})
    want = "fail" if native_windows else "ok"
    if platform.get("status") != want:
        problems.append(f"WS001 platform: {platform.get('status')}, expected {want}")
    if native_windows and "WSL2" not in platform.get("fix", ""):
        problems.append("WS001 does not point at WSL2")
    rows = [f"| {c['id']} | {c['role']} | {c['status']} |" for c in report.get("checks", [])]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### ws doctor on {report['os']['kind']} ({report['os']['release']}): exit {code}\n\n")
            f.write("| id | role | status |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    for p in problems:
        print(f"FAIL: {p}")
    if not problems:
        print(f"pass: exit {code}, {len(ROLES)} roles unconfigured on {report['os']['kind']}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
