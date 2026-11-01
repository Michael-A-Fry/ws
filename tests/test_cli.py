"""ws doctor, ws init, OS detection, the profile writer and the usage check. No network: the usage check runs
against a local stub server."""

from __future__ import annotations

import json
import threading
import tomllib
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from ws import cli, doctor, osinfo, usage
from ws.profile import dumps, profile_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    return tmp_path


def doctor_json(capsys, *extra) -> tuple[int, dict]:
    code = cli.main(["doctor", "--json", *extra])
    return code, json.loads(capsys.readouterr().out)["data"]


def by_id(rep: dict) -> dict:
    return {c["id"]: c for c in rep["checks"]}


def linux(**kw) -> osinfo.OSInfo:
    base = {"kind": "linux", "release": "ubuntu 24.04", "arch": "x86_64", "scheduler": "systemd", "supported": True}
    return osinfo.OSInfo(**{**base, **kw})


def test_fresh_doctor_lists_every_role_unconfigured_and_exits_1(home, capsys):
    code, rep = doctor_json(capsys)
    assert code == 1
    assert rep["ok"] is False and rep["schema"] == 1
    checks = by_id(rep)
    for role in doctor.ROLES:
        assert checks[role.id]["status"] == "unconfigured", role.name
        assert checks[role.id]["role"] == role.name
        assert f"[roles.{role.name}]" in checks[role.id]["fix"]
    assert checks["WS003"]["status"] == "unconfigured"
    assert checks["WS004"]["status"] == "unconfigured"


def test_check_ids_are_unique_and_stable(home, capsys):
    _, rep = doctor_json(capsys)
    ids = [c["id"] for c in rep["checks"]]
    assert len(ids) == len(set(ids))
    assert ids[:4] == ["WS001", "WS002", "WS003", "WS004"]
    assert [r.id for r in doctor.ROLES] == [f"WS1{n:02d}" for n in range(1, 11)]


def test_text_output_shows_every_id_and_a_fix(home, capsys):
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    for role in doctor.ROLES:
        assert role.id in out
    assert "fix: run `ws init`" in out
    assert "unconfigured" in out.splitlines()[-1]


def test_init_answers_the_three_questions_and_is_idempotent(home, capsys):
    notes = home / "Notes"
    args = ["init", "--yes", "--stays-on", "no", "--server", "none", "--notes", str(notes), "--create-notes"]
    assert cli.main(args) == 0
    assert "wrote" in capsys.readouterr().out
    prof = tomllib.loads(profile_path().read_text())
    assert prof["machine"]["stays_on"] is False
    assert prof["machine"]["server"] == ""
    assert prof["roles"]["notes"]["path"] == str(notes)
    assert cli.main(args) == 0
    assert "0 changes" in capsys.readouterr().out
    code, rep = doctor_json(capsys)
    assert code == 1
    checks = by_id(rep)
    assert checks["WS003"]["status"] == checks["WS004"]["status"] == checks["WS106"]["status"] == "ok"
    assert checks["WS101"]["status"] == "unconfigured"


def test_init_keeps_roles_already_set(home, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text("[roles.budget]\nmonthly_cap_usd = 20\n")
    assert cli.main(["init", "--yes", "--notes", str(home), "--server", "lab.example.com"]) == 0
    prof = tomllib.loads(p.read_text())
    assert prof["roles"]["budget"]["monthly_cap_usd"] == 20
    assert prof["machine"]["server"] == "lab.example.com"


def test_init_refuses_a_missing_notes_folder_without_create(home, capsys):
    assert cli.main(["init", "--yes", "--notes", str(home / "nope")]) == 1
    assert not profile_path().exists()


def test_init_rejects_a_bad_yes_no(home, capsys):
    assert cli.main(["init", "--yes", "--stays-on", "maybe", "--notes", str(home)]) == 2


def test_broken_profile_reports_instead_of_crashing(home, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text("this is = = not toml\n")
    code, rep = doctor_json(capsys)
    assert code == 1
    assert by_id(rep)["WS003"]["status"] == "fail"


def test_detect_wsl_linux_macos_and_windows(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    (tmp_path / "proc/sys/kernel").mkdir(parents=True)
    (tmp_path / "proc/sys/kernel/osrelease").write_text("6.6.87.2-microsoft-standard-WSL2\n")
    wsl = osinfo.detect("Linux", {}, tmp_path)
    assert (wsl.kind, wsl.release, wsl.supported, wsl.scheduler) == ("wsl", "ubuntu 24.04", True, "none")
    (tmp_path / "proc/sys/kernel/osrelease").write_text("6.1.0-cloud-amd64\n")
    (tmp_path / "run/systemd/system").mkdir(parents=True)
    lin = osinfo.detect("Linux", {}, tmp_path)
    assert (lin.kind, lin.scheduler) == ("linux", "systemd")
    assert osinfo.detect("Linux", {"WSL_DISTRO_NAME": "Ubuntu-24.04"}, tmp_path).kind == "wsl"
    assert osinfo.detect("Darwin", {}, tmp_path).scheduler == "launchd"
    win = osinfo.detect("Windows", {}, tmp_path)
    assert (win.kind, win.supported) == ("windows", False)


def test_native_windows_fails_ws001_with_the_wsl_fix(tmp_path):
    win = osinfo.OSInfo("windows", "11", "amd64", "none", False)
    first = doctor.run_checks(win, tmp_path / "p.toml", env={})[0]
    assert (first.id, first.status) == ("WS001", "fail")
    assert "WSL2" in first.fix


def test_profile_writer_round_trips_awkward_strings():
    data = {
        "top": 1,
        "machine": {"notes": 'C:\\Users\\a "b"\n\tc ✓\x7f', "stays_on": True, "cap": 2.5},
        "roles": {"notes": {"path": "~/N"}, "odd key": {"list": ["a", "b"]}},
    }
    assert tomllib.loads(dumps(data)) == data


@pytest.mark.parametrize(("cap", "status"), [(0, "fail"), (-5, "fail"), (True, "fail"), ("20", "fail"), (20, "ok")])
def test_budget_needs_a_positive_number(cap, status):
    assert doctor._budget({"monthly_cap_usd": cap}, linux(), {}, True)[0] == status


def test_scheduler_must_match_the_machine():
    assert doctor._scheduler({"adapter": "launchd"}, linux(), {}, True)[0] == "fail"
    assert doctor._scheduler({"adapter": "systemd"}, linux(), {}, True)[0] == "ok"
    assert doctor._scheduler({"adapter": "cron"}, linux(), {}, True)[0] == "fail"


def test_runner_looks_for_the_binary(tmp_path):
    assert doctor._runner({"adapter": "opencode"}, linux(), {"PATH": str(tmp_path)}, True)[0] == "fail"
    exe = tmp_path / "opencode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert doctor._runner({"adapter": "opencode"}, linux(), {"PATH": str(tmp_path)}, True)[0] == "ok"


class _Stub(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"
    seen: ClassVar[list] = []

    def do_GET(self):
        if self.path.startswith("/elsewhere"):
            _Stub.seen.append(("elsewhere", self.headers.get("x-api-key"), None))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": []}')
            return
        if self.status in (301, 302, 307, 308):
            self.send_response(self.status)
            self.send_header("location", "/elsewhere")
            self.end_headers()
            return
        _Stub.seen.append((self.path, self.headers.get("x-api-key"), self.headers.get("anthropic-version")))
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Stub.seen = []
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


KEY = "sk-ant-admin01-" + "x" * 24


def test_usage_reads_the_report(stub):
    _, base = stub
    _Stub.status = 200
    _Stub.body = json.dumps(
        {"data": [{"results": [{"uncached_input_tokens": 1200, "cache_read_input_tokens": 300, "output_tokens": 45}]}]}
    ).encode()
    status, detail = usage.check_anthropic(KEY, base, now=datetime(2026, 11, 7, 9, tzinfo=UTC))
    assert status == "ok" and "1,545 tokens" in detail
    path, key, ver = _Stub.seen[0]
    assert path.startswith("/v1/organizations/usage_report/messages?starting_at=2026-11-06T00%3A00%3A00Z")
    assert (key, ver) == (KEY, "2023-06-01")
    assert KEY not in detail


def test_usage_refused_key_fails_without_echoing_it(stub):
    _, base = stub
    _Stub.status = 401
    _Stub.body = b'{"error": {}}'
    status, detail = usage.check_anthropic(KEY, base)
    assert status == "fail" and "401" in detail and KEY not in detail


def test_usage_rejects_a_normal_api_key_before_any_call(stub):
    _, base = stub
    status, _ = usage.check_anthropic("sk-ant-api03-" + "y" * 24, base)
    assert status == "fail" and _Stub.seen == []


def test_usage_role_wiring(stub, monkeypatch):
    _, base = stub
    monkeypatch.setenv("WS_ANTHROPIC_API_BASE", base)
    monkeypatch.setenv("WS_TEST_ALLOW_LOOPBACK_BASE", "1")  # a local stub is honoured only with the test flag
    _Stub.status, _Stub.body = 200, b'{"data": []}'
    cfg = {"adapter": "anthropic", "key_env": "WS_TEST_ADMIN"}
    assert doctor._usage(cfg, linux(), {}, True)[0] == "fail"
    assert doctor._usage(cfg, linux(), {"WS_TEST_ADMIN": KEY}, False)[0] == "warn"
    assert doctor._usage(cfg, linux(), {"WS_TEST_ADMIN": KEY}, True)[0] == "ok"
    assert doctor._usage({"adapter": "none"}, linux(), {}, True)[0] == "warn"


def test_package_has_no_runtime_dependencies():
    meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["dependencies"] == []


def test_usage_never_sends_the_key_over_plain_http_off_localhost():
    status, detail = usage.check_anthropic(KEY, "http://usage.example.com")
    assert status == "fail" and "https" in detail


def test_usage_key_with_a_newline_never_reaches_the_output(stub):
    _, base = stub
    _Stub.status, _Stub.body = 200, b'{"data": []}'
    assert usage.check_anthropic(KEY + "\n", base)[0] == "ok"
    status, detail = usage.check_anthropic(KEY[:20] + "\r\n" + KEY[20:], base)
    assert status == "fail" and KEY[20:] not in detail


def test_usage_does_not_follow_a_redirect_with_the_key(stub):
    _, base = stub
    _Stub.status = 307
    status, detail = usage.check_anthropic(KEY, base)
    assert status == "fail" and "redirect" in detail
    assert all(where != "elsewhere" for where, *_ in _Stub.seen)


def test_crashing_check_withholds_the_message(home, monkeypatch):
    def boom(*a):
        raise ValueError("secret-" + "value")

    role = doctor.ROLES[0]
    monkeypatch.setattr(doctor, "ROLES", (doctor.Role(role.id, role.name, role.what, role.example, boom),))
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text('[roles.runner]\nadapter = "opencode"\n')
    c = doctor.run_checks(linux(), p, env={})[-1]
    assert c.status == "fail" and "secret-value" not in c.detail


def test_init_keeps_extra_machine_keys_and_stores_an_absolute_notes_path(home, monkeypatch, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text('[machine]\nnickname = "desk"\n')
    (home / "n").mkdir()
    monkeypatch.chdir(home)
    assert cli.main(["init", "--yes", "--notes", "n"]) == 0
    prof = tomllib.loads(p.read_text())
    assert prof["machine"]["nickname"] == "desk"
    assert prof["machine"]["notes"] == str((home / "n").resolve())


def test_init_reports_a_roles_notes_that_is_not_a_table(home, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text('[roles]\nnotes = "oops"\n')
    assert cli.main(["init", "--yes", "--notes", str(home)]) == 1
    assert "must be tables" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        "https://usage.example.com",
        "https://api.anthropic.com.usage.example.com",
        "https://api.anthropic.com" + "@" + "usage.example.com",
        "https://user:pw" + "@" + "api.anthropic.com",
        "http://api.anthropic.com",
        "https://api.anthropic.com:8443",
        "https://api.anthropic.com/x",
        "https://api.anthropic.com?x=1",
        "https://api.anthropic.com\nx",
        "https://api.anthropic.com\\@usage.example.com",
        "http://127.0.0.1:9",
        "https://localhost",
    ],
)
def test_the_usage_base_url_from_the_environment_must_be_the_providers_own_host(value):
    with pytest.raises(usage.BaseRefused) as e:
        usage.env_base({"WS_ANTHROPIC_API_BASE": value})
    assert e.value.kind == "refused" and "WS_ANTHROPIC_API_BASE" in str(e.value) and e.value.hint


@pytest.mark.parametrize(
    "value", ["", "https://api.anthropic.com", "https://API.anthropic.com/", "https://api.anthropic.com:443"]
)
def test_the_usage_base_url_accepts_the_providers_own_host(value):
    assert urlsplit(usage.env_base({"WS_ANTHROPIC_API_BASE": value})).hostname == "api.anthropic.com"


def test_a_loopback_usage_base_needs_the_test_flag():
    env = {"WS_ANTHROPIC_API_BASE": "http://127.0.0.1:8123"}
    with pytest.raises(usage.BaseRefused):
        usage.env_base(env)
    assert usage.env_base({**env, "WS_TEST_ALLOW_LOOPBACK_BASE": "1"}) == "http://127.0.0.1:8123"


def test_a_refused_usage_base_sends_nothing_and_never_shows_the_key(monkeypatch):
    def never(*a, **k):
        raise AssertionError("the key must not be sent")

    monkeypatch.setattr(usage._OPENER, "open", never)
    monkeypatch.delenv("WS_TEST_ALLOW_LOOPBACK_BASE", raising=False)
    monkeypatch.setenv("WS_ANTHROPIC_API_BASE", "https://usage.example.com")
    status, detail = usage.check_anthropic(KEY)
    assert status == "fail" and "WS_ANTHROPIC_API_BASE" in detail and KEY not in detail
