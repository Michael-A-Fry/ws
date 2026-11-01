"""Builders for the home tests: a tmp HOME laid out the way each source expects it, and an injected `claude`.

Every value is made at run time under `tmp_path`; nothing reads the real HOME, the clock or a program.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ws.home import api

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC).timestamp()
SID = "11111111-aaaa-bbbb-cccc-000000000001"
SID2 = "22222222-aaaa-bbbb-cccc-000000000002"


def iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(NOW + offset_s, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Box:
    """A tmp HOME with the folders the sources look in, and helpers to put files there."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.ws = self.home / ".ws"
        self.claude = self.home / ".claude"
        self.usage = self.home / ".claude-usage"
        self.home.mkdir()
        self.environ = {"HOME": str(self.home), "WS_HOME": str(self.ws), "PATH": ""}

    def profile(self, text: str) -> None:
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / "profile.toml").write_text(text, encoding="utf-8")

    def json(self, path: Path, obj: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def lines(self, path: Path, objs: list, raw: str = "") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(o) + "\n" for o in objs) + raw, encoding="utf-8")
        return path

    def ws_session(self, sid: str = SID, cwd: str | None = None, runner: str = "claude-code") -> Path:
        cwd = cwd if cwd is not None else str(self.home / "code" / "app")
        rec = {"schema": 1, "ts": iso(-60), "session_id": sid, "release": "0.1.5", "started_at": iso(-3600),
               "runner": runner, "cwd": cwd}  # fmt: skip
        return self.json(self.ws / "sessions" / f"{sid}.json", rec)

    def pid_file(self, pid: int, sid: str = SID, tmux: str = "ws:@1.%1", **more) -> Path:
        rec = {"pid": pid, "sessionId": sid, "tmux": tmux, "status": "idle", "statusUpdatedAt": int((NOW - 30) * 1000)}
        return self.json(self.claude / "sessions" / f"{pid}.json", {**rec, **more})

    def transcript(self, sid: str, lines: list, folder: str = "-home-user-code-app") -> Path:
        return self.lines(self.claude / "projects" / folder / f"{sid}.jsonl", lines)

    def env(self, agents=None, run=None) -> api.Env:
        """An Env whose `claude agents --json` prints `agents` (a list), or whatever `run` does."""
        if run is None:

            def run(argv, timeout_s):
                assert argv[:3] == ["claude", "agents", "--json"] and timeout_s > 0
                return 0, json.dumps(agents if agents is not None else [])

        return api.Env.make(self.environ, run=run, now=NOW, host="testhost")


def agent(sid: str = SID, cwd: str = "/work/app", **more) -> dict:
    return {"pid": 4242, "cwd": cwd, "kind": "interactive", "startedAt": int((NOW - 3600) * 1000), "sessionId": sid,
            "name": "builder", "status": "busy", **more}  # fmt: skip
