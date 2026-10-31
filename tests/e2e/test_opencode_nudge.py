"""The stale-session nudge on a real OpenCode run (phase 4; ws.session). The shim plugin records the release a session
began on at `session.created` and asks `ws session nudge` at `session.idle`. Until now that was unverified against the
real runner: the event names came from the plugin docs.

Three `opencode run` calls against a stub model, the second and third continuing the first session:
  1. WS_RELEASE=old1: the session is recorded on that release, nothing is said;
  2. WS_RELEASE=old2: the nudge line is printed, once, and the record is marked;
  3. WS_RELEASE=old2 again: nothing more is said.
A thin wrapper stands in for `ws` in the generated shim so the nudge line (which the shim only turns into a toast the
headless runner has nowhere to show) lands in a log the test can read.

A missing runner skips the test, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import json
import shlex
import stat
import sys

from e2e import test_gate_runners as gr
from e2e.stubs import OpenAIStub

ws_home = gr.ws_home
work = gr.work


def wrapper(tmp_path, log) -> str:
    """A `ws` that behaves as `python -m ws`, and copies the output of `session nudge` to `log`."""
    script = tmp_path / "ws-wrapper"
    py = shlex.quote(sys.executable)
    script.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        f'  *" session nudge "*) {py} -m ws "$@" | tee -a {shlex.quote(str(log))} ;;\n'
        f'  *) exec {py} -m ws "$@" ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def generate(tmp_path, ws_cmd: str):
    from ws import cli

    out = tmp_path / "wshome" / "gate" / "opencode"
    assert cli.main(["gate", "generate", "--runner", "opencode", "--out", str(out), "--ws-cmd", ws_cmd]) == 0
    return out


def records(tmp_path) -> list[dict]:
    folder = tmp_path / "wshome" / "sessions"
    return [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def step(tmp_path, out, work, release: str, again: bool):
    with OpenAIStub([]) as stub:
        proc = gr.opencode_run(
            tmp_path,
            stub,
            out,
            work,
            extra_env={"WS_RELEASE": release},
            extra_args=("--continue",) if again else (),
        )
    assert proc.returncode == 0, gr.where(proc)
    return proc


def test_opencode_session_on_an_old_release_is_nudged_once(tmp_path, work):
    log = tmp_path / "nudges.log"
    out = generate(tmp_path, wrapper(tmp_path, log))

    step(tmp_path, out, work, "old1", again=False)
    recs = records(tmp_path)
    assert len(recs) == 1, f"session.created never reached ws session start: {recs}"
    assert recs[0]["runner"] == "opencode" and recs[0]["release"].endswith("+old1"), recs
    assert not log.exists() or log.read_text().strip() == "", "the nudge fired on the release it started on"

    step(tmp_path, out, work, "old2", again=True)
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()] if log.exists() else []
    assert len(lines) == 1, f"expected one nudge line after the release moved, got {lines}"
    assert "old2 is out" in lines[0] and "started on" in lines[0] and "old1" in lines[0], lines
    assert records(tmp_path)[0].get("nudged") is True

    step(tmp_path, out, work, "old2", again=True)
    assert len([ln for ln in log.read_text().splitlines() if ln.strip()]) == 1, "the nudge repeated"


def test_no_nudge_when_the_release_has_not_moved(tmp_path, work):
    log = tmp_path / "nudges.log"
    out = generate(tmp_path, wrapper(tmp_path, log))
    step(tmp_path, out, work, "old1", again=False)
    step(tmp_path, out, work, "old1", again=True)
    assert not log.exists() or log.read_text().strip() == ""
