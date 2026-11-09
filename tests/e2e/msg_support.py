"""Shared set-up for the message-delivery end-to-end tests: a scratch ws, a `ws` launcher that reads that scratch ws's
folders as "the account's own" (a real runner's hooks can't be told another home by the system), a policy with the inbox
guard on, and a sender that is a registered session in the scratch registry."""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

from ws import cli, msg, session

LAUNCHER = """\
import json, os, sys
from ws import session
env = json.loads(os.environ["WS_E2E_TRUSTED"])
session.trusted_env = lambda: env  # the scratch folders stand in for the account's own
from ws.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def policy_text() -> str:
    """The built-in default policy with the inbox and presence guards turned on."""
    from ws import policy

    text = policy.default_toml()
    old = 'on = ["syntax-check", "session-context", "dev-server"]'
    assert old in text
    return text.replace(old, 'on = ["inbox", "presence", "dev-server"]')


def trusted(tmp_path) -> dict:
    return {"HOME": str(tmp_path / "home"), "WS_HOME": str(tmp_path / "wshome")}


def setup(tmp_path, runner: str, monkeypatch) -> tuple[Path, dict]:
    """(the generated config folder for `runner`, the extra environment the runner needs)."""
    (tmp_path / "wshome").mkdir(exist_ok=True)
    (tmp_path / "wshome" / "policy.toml").write_text(policy_text())
    script = tmp_path / "wsx.py"
    script.write_text(LAUNCHER)
    ws_cmd = shlex.join([sys.executable, str(script)])
    out = tmp_path / "wshome" / "gate" / runner
    argv = ["gate", "generate", "--runner", runner, "--out", str(out), "--ws-cmd", ws_cmd]
    assert cli.main([*argv, "--policy", str(tmp_path / "wshome" / "policy.toml")]) == 0
    env = {"WS_E2E_TRUSTED": json.dumps(trusted(tmp_path)), "WS_GATE_DAEMON": "0"}
    monkeypatch.setattr(session, "trusted_env", lambda: trusted(tmp_path))  # this process reads the same folders
    return out, env


def receiver(tmp_path, timeout: float = 60.0) -> str:
    """The id of the session the runner has started (its hook wrote the record and the identity)."""
    folder = tmp_path / "wshome" / "identity"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = [p.stem for p in folder.glob("*.json")] if folder.is_dir() else []
        if found:
            return found[0]
        time.sleep(0.2)
    raise AssertionError("the runner never started a session")


def send_from_another_session(monkeypatch, to: str, body: str, **kw) -> dict:
    """Send `body` to `to` as a registered worker session called `sender-1` (the scratch registry's)."""
    sender = session.Record(session_id="sender-1", release="e2e", cwd=str(Path.cwd()), role="worker")
    monkeypatch.setattr(session, "resolve_caller", lambda session_id=None: sender)
    return msg.send(to, "note", body, **kw)


def requests_text(stub) -> list[str]:
    return [json.dumps(r) for r in stub.requests]


__all__ = ["receiver", "requests_text", "send_from_another_session", "setup"]
