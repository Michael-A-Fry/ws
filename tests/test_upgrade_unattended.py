"""`ws upgrade --unattended`: the service proves a release itself, waits for idle, swaps, checks and rolls back.

A tmp HOME holds a real `ws install` (so the hook probes meet the real gate), a stand-in `ws` the stub installer
"installs" (its version is a file), and a fake release tree: a git repository with a really signed tag (a throwaway key),
assets with their hashes, and bundles a stub `cosign` verifies by digest and identity. Nothing reaches the network or
GitHub; the request is a file in a tmp state folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import release_fakes
from helpers.release_fakes import GIT, IDENTITY, ISSUER, KEYGEN, NEEDS, TAG, keygen, sha, stub_tools, trust_root

from ws import agentsession, approvals, cli, paths, queue, selfcmd, upgrade, upgradecheck, upgradeproof, version
from ws.term import bridge

pytestmark = pytest.mark.skipif(GIT is None or KEYGEN is None, reason=NEEDS)

OLD, NEW = "0.1.11.6", "0.1.11.8"

STUB_WS = """import os, sys
state = os.environ["STUB_STATE"]
if sys.argv[1:] == ["--version"]:
    print("ws " + open(os.path.join(state, "version")).read().strip())
elif sys.argv[1:] == ["upgrade", "--hooks-only"]:
    fail = os.path.join(state, "hooks_fail")
    if os.path.exists(fail) and open(os.path.join(state, "version")).read().strip() != "0.1.11.6":
        sys.exit(int(open(fail).read().strip() or 1))  # the new ws's hooks step fails (with the code in the file); the old one works
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


def make_release(root: Path, key: Path, **kw) -> release_fakes.Release:
    """A fake release whose installer is this file's stub."""
    return release_fakes.Release(root, key, installer=INSTALLER.encode(), **kw)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """The service's world: a tmp HOME with ws installed, stub tools first on the PATH, a trust root, the old ws installed."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "stubstate"
    state.mkdir()
    (state / "version").write_text(OLD, encoding="utf-8")
    tools = stub_tools(tmp_path / "tools")
    stubws = tmp_path / "stubws.py"
    stubws.write_text(STUB_WS, encoding="utf-8")
    key = tmp_path / "key"
    blob = keygen(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("WS_GATE_DAEMON", "0")
    monkeypatch.setenv(
        "PATH", f"{home / '.local' / 'bin'}{os.pathsep}{tools}{os.pathsep}{os.environ['PATH']}"
    )  # a session's own first
    # the tools the proof runs come from system folders only (stubs for cosign and uv, the real git); the installer may see
    # these test variables and the allow-list's names, nothing else
    monkeypatch.setattr(upgradeproof, "tool_dirs", lambda: [str(tools), "/usr/bin", "/bin"])
    monkeypatch.setattr(upgradeproof, "ENV_KEEP", (*upgradeproof.ENV_KEEP, "INSTALL_LOG", "INSTALL_ENV", "STUB_STATE"))
    monkeypatch.setattr(
        upgrade, "_own_cgroup", lambda: "0::/user.slice/user@1000.service/app.slice/ws-upgrade.service\n"
    )
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
    root = trust_root(blob)
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
    world.release = lambda **kw: make_release(tmp_path / f"rel{len(list(tmp_path.glob('rel*')))}", key, **kw)
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


def serve(svc, release: release_fakes.Release | None = None, text: str = TAG, **kw) -> int:
    request(svc, text)
    return upgrade.cmd_unattended(args(**kw), os.environ, source=release or svc.release(), sleep=lambda s: None)


def installed(svc) -> str:
    return (svc.state / "version").read_text().strip()


def rows() -> list[dict]:
    return upgradecheck.last_rows(os.environ)


def history() -> list[tuple]:
    """The outcome lines; the `swap-start` and `swap-abandoned` markers are `marks()`."""
    return [
        (d["verb"], d["from"], d["to"])
        for d in upgrade.history(os.environ)
        if d["verb"] not in ("swap-start", "swap-abandoned")
    ]


def marks() -> list[str]:
    return [d["verb"] for d in upgrade.history(os.environ) if d["verb"] in ("swap-start", "swap-abandoned")]


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


def test_mirror_env_ignored(svc, monkeypatch):
    """The installer is run with an allow-list: the mirror names, and any other variable (a proxy, a library path, a git
    switch) a session could have set for the service, never reach it."""
    planted = {
        *upgrade.MIRROR_VARS,
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "LD_PRELOAD",
        "PYTHONPATH",
        "GIT_SSH_COMMAND",
        "BASH_ENV",
        "ENV",
        "NODE_OPTIONS",
    }
    for name in planted:
        monkeypatch.setenv(name, "https://evil.example/")
    assert serve(svc) == 0 and installed(svc) == NEW
    seen = dict(line.split("=", 1) for line in Path(os.environ["INSTALL_ENV"]).read_text().splitlines() if "=" in line)
    assert not planted & seen.keys()
    allowed = {*upgradeproof.ENV_KEEP, "HOME", "PATH", "PWD", "SHLVL", "OLDPWD", "_", "WS_UPGRADE_STEP"}
    assert set(seen) <= allowed, set(seen) - allowed
    assert seen["PATH"] == os.pathsep.join(upgradeproof.tool_dirs()) and seen["HOME"] == os.environ["HOME"]


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
    """The target is read from the upgrade history, never from the request; its installer is the release's own, proved."""
    monkeypatch.setattr(
        upgrade, "fetch_installer", lambda *a, **k: pytest.fail("a rollback fetched an installer unproved")
    )
    upgrade.record(os.environ, "promote", "0.1.11.4", OLD)
    code = serve(svc, svc.release(tag="v0.1.11.4"), text="rollback")
    assert code == 0 and installed(svc) == "0.1.11.4"
    assert Path(os.environ["INSTALL_LOG"]).read_text().split()[-2:] == ["--version", "v0.1.11.4"]
    assert len(Path(svc.tools / "cosign.log").read_text().splitlines()) == 2  # N8: proved like an upgrade
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
    release = make_release(svc.tmp / "relx", other)
    assert serve(svc, release) == upgrade.EXIT_REFUSED and installed(svc) == OLD
    assert f"{TAG} is not signed by a key in the trust root" in rows()[-1]["reason"]


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


# ---- B4: the proof runs system tools with a home and an environment of its own ----------------------------------------------


def fake(svc, name: str, body: str = "exit 0") -> Path:
    """A program in the person's own ~/.local/bin (first on the PATH here), which leaves a marker if it is ever run."""
    mark = svc.tmp / f"ran-{name}"
    path = svc.home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {mark}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return mark


@pytest.mark.parametrize("name", ["git", "cosign", "uv", "ssh-keygen", "bash"])
def test_a_program_planted_on_the_path_is_never_run(svc, name):
    release = svc.release()  # made before the fake exists: making a signed tag runs ssh-keygen too
    mark = fake(svc, name)
    assert (
        serve(svc, release) == 0 and installed(svc) == NEW
    )  # the good release still goes through, with the system's tools
    assert not mark.exists()


# ---- B5 and N10: every failure after the swap has started is rolled back, and each is the specific refusal -------------------


def test_a_hooks_rewrite_that_fails_is_rolled_back_not_left_half_done(svc):
    (svc.state / "hooks_fail").write_text("")
    assert serve(svc) == upgrade.EXIT_FAILED
    assert installed(svc) == OLD and history() == [("auto-rollback", OLD, OLD)]
    assert rows()[-1]["verb"] == "auto-rollback" and rows()[-1]["result"] == "rolled-back"
    assert any(d["step"] == "hooks" and d["exit"] == 1 for d in _log())


def test_a_hooks_step_that_could_not_put_files_back_is_rolled_back_too(svc):
    """B5: EXIT_NOT_RESTORED from the hooks step is a non-zero after the swap like any other: the new ws is not left."""
    (svc.state / "hooks_fail").write_text(str(upgrade.EXIT_NOT_RESTORED))
    assert serve(svc) == upgrade.EXIT_FAILED
    assert installed(svc) == OLD and history() == [("auto-rollback", OLD, OLD)]
    assert [(r["verb"], r["result"]) for r in rows()] == [("auto-rollback", "rolled-back")]
    assert any(d["step"] == "hooks" and d["exit"] == upgrade.EXIT_NOT_RESTORED for d in _log())


def test_a_way_back_that_cannot_restore_the_files_is_never_reported_clean(svc, monkeypatch):
    (svc.state / "hooks_fail").write_text("")
    monkeypatch.setattr(upgrade._Held, "restore", lambda self: [svc.home / ".ws" / "policy.toml"])
    assert serve(svc) == upgrade.EXIT_NOT_RESTORED
    assert installed(svc) == OLD  # the old ws is back; the file that was not is named, and the exit says so
    assert [(r["verb"], r["result"]) for r in rows()] == [("auto-rollback", "not-restored")]


def test_an_installer_that_fails_is_rolled_back_when_no_person_is_looking(svc):
    (svc.state / "fail").write_text("")  # the stub installer exits 1, for the upgrade and for the way back
    assert serve(svc) != 0
    assert history()[0][0] == "auto-rollback" and rows()[-1]["verb"] == "auto-rollback"


def test_a_crash_mid_swap_leaves_a_way_back_that_goes_one_release_not_two(svc, monkeypatch):
    upgrade.record(os.environ, "promote", "0.1.11.4", OLD)  # an earlier upgrade: the release before OLD
    monkeypatch.setattr(upgradecheck, "acceptance", lambda *a, **k: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        serve(svc)
    assert marks() == ["swap-start"] and installed(svc) == NEW
    assert upgrade.previous_release(os.environ) == OLD  # not 0.1.11.4


# ---- N6, N7, N8 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"ppid": 1}, "pid 1"),
        ({"parent_comm": "bash"}, "the parent here is bash"),
        ({"cgroup": "0::/user.slice/session-3.scope\n"}, "inside ws-upgrade.service"),
        ({"cgroup": ""}, "inside ws-upgrade.service"),
    ],
)
def test_unattended_needs_the_user_manager_as_parent_and_the_units_own_cgroup(svc, kw, why):
    base = {"parent_comm": "systemd", "ppid": 4242, "cgroup": "0::/a/b/ws-upgrade.service\n"}
    assert upgrade.unattended_refusal(os.environ, **base) is None
    assert why in upgrade.unattended_refusal(os.environ, **{**base, **kw})


def test_the_unattended_flag_is_not_accepted_abbreviated(svc, capsys):
    for flag in ("--unatt", "--unattend", "--u"):
        assert cli.main(["upgrade", flag]) == 2  # a usage error, never the unit's entry
    assert capsys.readouterr().err.count("unrecognized arguments") == 3
    assert not upgrade.request_path(os.environ).exists()


def test_a_rollback_to_a_release_with_no_signed_installer_is_refused_and_nothing_is_fetched(svc, monkeypatch):
    monkeypatch.setattr(upgrade, "fetch_installer", lambda *a, **k: pytest.fail("fetched an installer unproved"))
    upgrade.record(os.environ, "promote", "0.1.11.4", OLD)
    release = svc.release(tag="v0.1.11.4")
    for name in ("install.sh", "install.sh.sha256", "install.sh.sigstore.json"):
        del release.assets[name]  # a release from before the installer was an asset
    assert serve(svc, release, text="rollback") == upgrade.EXIT_REFUSED
    assert "install.sh" in rows()[-1]["reason"] and not Path(os.environ["INSTALL_LOG"]).exists()
    assert installed(svc) == OLD
