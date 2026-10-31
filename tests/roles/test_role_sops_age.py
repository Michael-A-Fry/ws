"""Secrets role, sops-age: the check looks at tools, the key's owner and mode (never its content); reads shell out to a
fake sops. No real sops or age."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ws import secrets as wsecrets
from ws.roles import sops_age


@pytest.fixture
def tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for t in ("sops", "age"):
        (bin_dir / t).write_text("#!/bin/sh\n")
        (bin_dir / t).chmod(0o755)
    return {"PATH": str(bin_dir)}


@pytest.fixture
def files(tmp_path):
    f, k = tmp_path / "secrets.sops.yaml", tmp_path / "age-key.txt"
    f.write_text("enc")
    k.write_text("AGE-SECRET-KEY-PLACEHOLDER")
    k.chmod(0o600)
    return f, k


def cfg_for(files):
    return {"adapter": "sops-age", "file": str(files[0]), "key_file": str(files[1])}


def test_named_only_warns_that_the_broker_does_not_use_it_yet(linux):
    status, detail = sops_age.check({"adapter": "sops-age"}, linux, {}, False)
    assert status == "warn" and "not used by the broker yet" in detail and "file and key_file" in detail


def test_one_of_the_two_paths_is_a_failure(linux, files):
    assert sops_age.check({"file": str(files[0])}, linux, {}, False) == (
        "fail",
        "set both file and key_file (key_file is missing)",
    )


def test_sops_and_age_must_be_on_path(linux, files, tmp_path):
    status, detail = sops_age.check(cfg_for(files), linux, {"PATH": str(tmp_path)}, False)
    assert status == "fail" and "sops and age not on PATH" in detail


def test_the_key_in_the_same_user_is_a_warning(linux, files, tools):
    status, detail = sops_age.check(cfg_for(files), linux, tools, False)
    assert status == "warn" and "same user the agent runs as" in detail


def test_a_loose_key_fails(linux, files, tools):
    files[1].chmod(0o640)
    status, detail = sops_age.check(cfg_for(files), linux, tools, False)
    assert status == "fail" and "0640" in detail


def test_a_missing_key_or_file_fails(linux, files, tools, tmp_path):
    files[1].unlink()
    assert "does not exist" in sops_age.check(cfg_for(files), linux, tools, False)[1]
    files[0].unlink()
    assert "sops file" in sops_age.check(cfg_for(files), linux, tools, False)[1]


def test_a_good_setup_is_still_a_warning_while_the_broker_ignores_it(linux, files, tools, monkeypatch):
    real = Path.stat

    def stat(self, **kw):
        st = real(self, **kw)
        if self == files[1]:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.geteuid() + 1, st.st_gid, *st[6:]))
        return st

    monkeypatch.setattr(Path, "stat", stat)
    status, detail = sops_age.check(cfg_for(files), linux, tools, False)
    assert status == "warn" and "not used by the broker yet" in detail


def test_a_key_in_a_folder_we_cannot_enter_passes_the_key_check(linux, files, tools, monkeypatch):
    real = Path.stat

    def stat(self, **kw):
        if self == files[1]:
            raise PermissionError(13, "denied")
        return real(self, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    assert "not used by the broker yet" in sops_age.check(cfg_for(files), linux, tools, False)[1]


def test_the_check_never_opens_the_key(linux, files, tools, monkeypatch):
    real = Path.read_text
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **k: pytest.fail("read a file") if self == files[1] else real(self, *a, **k)
    )
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: pytest.fail("opened a file"))
    sops_age.check(cfg_for(files), linux, tools, False)


class FakeRun:
    def __init__(self, code=0, out="", err=""):
        self.calls, self.result = [], subprocess.CompletedProcess([], code, out, err)

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return self.result


def test_sops_store_reads_one_key_with_the_age_key_file(files):
    run = FakeRun(0, "a-value\n")
    store = sops_age.SopsStore(files[0], files[1], run)
    assert store.read("anthropic") == "a-value"
    [(argv, kw)] = run.calls
    assert argv == ["sops", "decrypt", "--extract", '["anthropic"]', str(files[0])]
    assert kw["env"]["SOPS_AGE_KEY_FILE"] == str(files[1]) and set(kw["env"]) == {"PATH", "SOPS_AGE_KEY_FILE"}


def test_sops_store_errors_never_carry_output(files):
    store = lambda run: sops_age.SopsStore(files[0], files[1], run)
    with pytest.raises(FileNotFoundError):
        store(FakeRun(1, "", "Error: key 'x' not found")).read("x")
    with pytest.raises(wsecrets.SecretsError) as e:
        store(FakeRun(1, "leaked-value", "no matching creation rules: leaked-value")).read("x")
    assert "leaked-value" not in str(e.value)
    with pytest.raises(wsecrets.SecretsError, match="not a valid secret name"):
        store(FakeRun()).read('x"] --other [')
    with pytest.raises(wsecrets.SecretsError, match="empty"):
        store(FakeRun(0, "\n")).read("x")

    def missing(*a, **k):
        raise FileNotFoundError("sops")

    with pytest.raises(wsecrets.SecretsError, match="did not run"):
        store(missing).read("x")
