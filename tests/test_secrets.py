"""The secrets registry, the store, `ws secret` and doctor WS104. Planted values are built at run time and never
appear in any output, listing or error."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from ws import broker, cli, doctor, osinfo
from ws import secrets as wsecrets

VALUE = "planted-" + os.urandom(12).hex()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    h.mkdir()
    monkeypatch.setenv("WS_HOME", str(h))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    return h


def registry(home: Path, text: str) -> None:
    (home / "secrets.toml").write_text(text, encoding="utf-8")


GOOD = """
[anthropic]
hosts = ["api.anthropic.com"]

[github]
hosts = ["API.github.com"]
header = "Authorization"
purposes = ["review"]
"""


def run(capsys, *argv, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_registry_defaults_and_authorization_format(home):
    registry(home, GOOD)
    reg = wsecrets.load_registry(wsecrets.registry_path())
    a, g = reg["anthropic"], reg["github"]
    assert (a.header, a.format, a.purposes, a.hosts) == ("x-api-key", "{value}", (), ("api.anthropic.com",))
    assert (g.header, g.format, g.purposes, g.hosts) == (
        "authorization",
        "Bearer {value}",
        ("review",),
        ("api.github.com",),
    )


def test_missing_registry_is_empty(home):
    assert wsecrets.load_registry(home / "nope.toml") == {}


@pytest.mark.parametrize(
    "text",
    [
        "[bad name]\nhosts = []",
        "[1abc]\nhosts = []",
        "x = 1",
        '[a]\nhosts = ["https://api.example.com"]',
        '[a]\nhosts = ["api.example.com/v1"]',
        '[a]\nhosts = "api.example.com"',
        '[a]\nhosts = []\nheader = "bad header"',
        '[a]\nhosts = []\nformat = "no placeholder"',
        '[a]\nhosts = []\nformat = "{value} {value}"',
        "[a\nhosts = []",
    ],
)
def test_bad_registry_raises(home, text):
    registry(home, text)
    with pytest.raises(wsecrets.SecretsError):
        wsecrets.load_registry(wsecrets.registry_path())


def test_store_writes_0600_in_0700_and_leaves_no_temp_files(tmp_path):
    s = wsecrets.Store(tmp_path / "store")
    s.write("anthropic", VALUE)
    s.write("anthropic", VALUE + "2")  # replace
    assert stat.S_IMODE(s.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((s.path / "anthropic").stat().st_mode) == 0o600
    assert [p.name for p in s.path.iterdir()] == ["anthropic"]
    assert s.read("anthropic") == VALUE + "2"
    assert s.names() == ["anthropic"] and s.loose() == []


def test_store_read_fails_closed(tmp_path):
    s = wsecrets.Store(tmp_path / "store")
    with pytest.raises(wsecrets.SecretsError):  # no directory
        s.read("anthropic")
    s.write("anthropic", VALUE)
    with pytest.raises(FileNotFoundError):  # unset
        s.read("github")
    (s.path / "anthropic").chmod(0o640)
    with pytest.raises(wsecrets.SecretsError) as e:
        s.read("anthropic")
    assert VALUE not in str(e.value)
    (s.path / "anthropic").chmod(0o600)
    s.path.chmod(0o750)
    with pytest.raises(wsecrets.SecretsError):
        s.read("anthropic")
    assert len(s.loose()) == 1


def test_store_tightens_its_own_loose_directory_on_write(tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    d.chmod(0o755)
    wsecrets.Store(d).write("a", VALUE)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "a b", "x" * 80])
def test_store_rejects_names_that_could_leave_the_folder(tmp_path, name):
    s = wsecrets.Store(tmp_path / "store")
    with pytest.raises(wsecrets.SecretsError):
        s.write(name, VALUE)
    with pytest.raises(wsecrets.SecretsError):
        s.read(name)
    with pytest.raises(wsecrets.SecretsError):
        s.delete(name)


@pytest.mark.parametrize("bad", ["", "line1\nline2", "tab\there", "x" * 9000, "nul\x00"])
def test_values_that_could_not_be_a_header_are_refused(tmp_path, bad):
    with pytest.raises(wsecrets.SecretsError) as e:
        wsecrets.Store(tmp_path / "s").write("a", bad)
    assert "line1" not in str(e.value)


def test_non_utf8_file_does_not_leak(tmp_path):
    s = wsecrets.Store(tmp_path / "store")
    s.write("a", VALUE)
    (s.path / "a").write_bytes(b"\xff\xfe" + VALUE.encode())
    with pytest.raises(wsecrets.SecretsError) as e:
        s.read("a")
    assert VALUE not in str(e.value)


def test_set_list_rm_round_trip_never_shows_the_value(home, capsys, monkeypatch):
    registry(home, GOOD)
    code, out, err = run(capsys, "secret", "list")
    assert code == 0 and "unset" in out
    code, out, err = run(capsys, "secret", "set", "anthropic", stdin=VALUE + "\n", monkeypatch=monkeypatch)
    assert code == 0 and VALUE not in out + err
    assert wsecrets.Store(home / "secrets").read("anthropic") == VALUE  # newline stripped
    code, out, err = run(capsys, "secret", "list")
    assert code == 0 and VALUE not in out + err
    line = next(ln for ln in out.splitlines() if ln.startswith("anthropic"))
    assert " set " in line and "api.anthropic.com" in line
    code, out, _ = run(capsys, "secret", "list", "--json")
    rows = {r["name"]: r for r in json.loads(out)["secrets"]}
    assert rows["anthropic"]["state"] == "set" and rows["github"]["state"] == "unset"
    assert VALUE not in out
    code, out, _ = run(capsys, "secret", "rm", "anthropic")
    assert code == 0 and "removed" in out
    code, out, _ = run(capsys, "secret", "rm", "anthropic")
    assert code == 0 and "0 changes" in out


def test_set_strips_crlf_and_refuses_unregistered_or_bad_values(home, capsys, monkeypatch):
    registry(home, GOOD)
    code, _, _ = run(capsys, "secret", "set", "anthropic", stdin=VALUE + "\r\n", monkeypatch=monkeypatch)
    assert code == 0 and wsecrets.Store(home / "secrets").read("anthropic") == VALUE
    code, _, err = run(capsys, "secret", "set", "nothere", stdin=VALUE, monkeypatch=monkeypatch)
    assert code == 2 and "not in" in err and VALUE not in err
    code, _, err = run(capsys, "secret", "set", "github", stdin="a\nb\n", monkeypatch=monkeypatch)
    assert code == 1 and "control character" in err
    code, _, err = run(capsys, "secret", "set", "github", stdin="", monkeypatch=monkeypatch)
    assert code == 1 and "empty" in err


def test_set_prompts_with_getpass_on_a_terminal(home, capsys, monkeypatch):
    registry(home, GOOD)
    monkeypatch.setattr("sys.stdin", type("T", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr("getpass.getpass", lambda prompt: VALUE)
    code, out, err = run(capsys, "secret", "set", "anthropic")
    assert code == 0 and VALUE not in out + err
    assert wsecrets.Store(home / "secrets").read("anthropic") == VALUE


def test_store_path_comes_from_the_profile(home, tmp_path, capsys, monkeypatch):
    registry(home, GOOD)
    elsewhere = tmp_path / "elsewhere"
    (home / "profile.toml").write_text(
        f'[roles.secrets]\nadapter = "ws-secret"\nstore = "{elsewhere}"\n', encoding="utf-8"
    )
    code, *_ = run(capsys, "secret", "set", "anthropic", stdin=VALUE, monkeypatch=monkeypatch)
    assert code == 0 and (elsewhere / "anthropic").is_file()


def test_list_says_when_the_store_belongs_to_someone_else(home, capsys, monkeypatch):
    registry(home, GOOD)

    def deny(self):
        raise PermissionError

    monkeypatch.setattr(wsecrets.Store, "names", deny)
    code, out, _ = run(capsys, "secret", "list")
    assert code == 0 and "unknown" in out and "another user" in out


def linux_wall(monkeypatch, python=("/usr/bin/python3", "")):
    monkeypatch.setattr(osinfo, "detect", lambda: osinfo.OSInfo("linux", "ubuntu 24.04", "x86_64", "systemd", True))
    monkeypatch.setattr(wsecrets, "trusted_python", lambda: python)


def test_wall_prints_the_sudo_steps_on_linux(home, capsys, monkeypatch):
    registry(home, GOOD)
    linux_wall(monkeypatch)
    code, out, _ = run(capsys, "secret", "wall")
    assert code == 0
    for want in (
        "useradd",
        "ws-broker",
        "ws-clients",
        "systemctl enable --now ws-broker",
        "broker serve",
        "0700",
        "install -o root",
        "ExecStart=/opt/ws/bin/ws",  # the broker runs the root-owned copy, never the user's ws
        "install -d -o root -g root -m 0755 /opt/ws",
        "find ws -type f -name '*.py'",  # regular files only
    ):
        assert want in out
    assert str(home / "secrets.toml") in out and VALUE not in out


def test_wall_never_moves_old_values_and_says_to_rotate_them(home, capsys, monkeypatch):
    registry(home, GOOD)
    wsecrets.Store(home / "secrets").write("anthropic", VALUE)
    linux_wall(monkeypatch)
    code, out, _ = run(capsys, "secret", "wall")
    assert code == 0 and VALUE not in out
    assert "mv " not in out and "sh -c" not in out and "chown -R" not in out
    assert "readable by everything that ran as" in out and "revoke the old one" in out
    for name in ("anthropic", "github"):  # one paste command per registered name, with the broker user's WS_HOME
        assert f"sudo -u ws-broker env WS_HOME=/var/lib/ws-broker /opt/ws/bin/ws secret set {name}" in out
    assert f"rm -r {home / 'secrets'}" in out


def test_wall_quotes_every_path_from_the_users_side(tmp_path, capsys, monkeypatch):
    import shlex

    evil = tmp_path / "a b;$(touch x)'q"
    evil.mkdir()
    monkeypatch.setenv("WS_HOME", str(evil))
    registry(evil, GOOD)
    linux_wall(monkeypatch)
    code, out, _ = run(capsys, "secret", "wall")
    assert code == 0
    assert shlex.quote(str(evil / "secrets.toml")) in out
    assert shlex.quote(str(evil / "secrets")) in out
    assert str(evil / "secrets.toml") not in out.replace(shlex.quote(str(evil / "secrets.toml")), "")
    pkg_parent = str(Path(wsecrets.__file__).resolve().parent.parent)
    assert f"(cd {shlex.quote(pkg_parent)} && find ws" in out


def test_wall_takes_the_user_from_the_account_database_not_the_environment(home, capsys, monkeypatch):
    import pwd

    registry(home, GOOD)
    linux_wall(monkeypatch)
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    code, out, _ = run(capsys, "secret", "wall")
    me = pwd.getpwuid(os.getuid()).pw_name
    assert code == 0 and f"usermod -aG ws-clients {me}" in out and "someone-else" not in out


def test_wall_refuses_without_a_root_owned_python_and_prints_no_commands(home, capsys, monkeypatch):
    registry(home, GOOD)
    linux_wall(monkeypatch, python=(None, "/usr/bin is not root's"))
    code, out, err = run(capsys, "secret", "wall")
    assert code == 1 and out == "" and "only root can change" in err and "/usr/bin is not root's" in err


def test_wall_refuses_to_run_as_root(home, capsys, monkeypatch):
    linux_wall(monkeypatch)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    code, out, err = run(capsys, "secret", "wall")
    assert code == 1 and out == "" and "normal user" in err


def fake_ownership(monkeypatch, uid=0, strip=0o022):
    """Make every stat look root-owned (and optionally not group/other-writable), so the root-only check can be tested."""
    real_stat, real_lstat = Path.stat, Path.lstat

    def wrap(fn):
        def inner(self, *a, **k):
            st = fn(self, *a, **k)
            f = list(st)
            f[4] = uid
            f[0] = st.st_mode & ~strip
            return os.stat_result(f)

        return inner

    monkeypatch.setattr(Path, "stat", wrap(real_stat))
    monkeypatch.setattr(Path, "lstat", wrap(real_lstat))


def test_untrusted_reason_names_the_owner_and_the_writable_folder(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert "not root" in wsecrets.untrusted_reason(tmp_path)


def test_untrusted_reason_passes_a_root_owned_closed_tree(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.py").write_text("x = 1\n")
    fake_ownership(monkeypatch)
    assert wsecrets.untrusted_reason(tmp_path / "ws", tree=True) is None


def test_untrusted_reason_catches_group_writable_and_symlinks(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.py").write_text("x = 1\n")
    fake_ownership(
        monkeypatch, strip=0
    )  # root-owned, modes as they are on disk (a tmp folder is writable by its owner only)
    tmp_path.chmod(0o775)
    assert "other than root" in wsecrets.untrusted_reason(tmp_path / "ws")
    tmp_path.chmod(0o755)
    (tmp_path / "ws" / "link.py").symlink_to(tmp_path / "ws" / "a.py")
    fake_ownership(monkeypatch)
    assert "symlink" in wsecrets.untrusted_reason(tmp_path / "ws", tree=True)


def test_set_and_rm_hints_keep_ws_home(home, capsys, monkeypatch):
    registry(home, GOOD)

    def deny(self, *a):
        raise PermissionError

    monkeypatch.setattr(wsecrets.Store, "write", deny)
    monkeypatch.setattr(wsecrets.Store, "delete", deny)
    code, _, err = run(capsys, "secret", "set", "anthropic", stdin=VALUE, monkeypatch=monkeypatch)
    assert code == 1 and "sudo -u ws-broker env WS_HOME=/var/lib/ws-broker /opt/ws/bin/ws secret set anthropic" in err
    code, _, err = run(capsys, "secret", "rm", "anthropic")
    assert code == 1 and "sudo -u ws-broker env WS_HOME=/var/lib/ws-broker /opt/ws/bin/ws secret rm anthropic" in err


@pytest.mark.parametrize("kind", ["macos", "wsl"])
def test_wall_says_what_is_not_supported_elsewhere(home, capsys, monkeypatch, kind):
    monkeypatch.setattr(osinfo, "detect", lambda: osinfo.OSInfo(kind, "x", "arm64", "none", True))
    code, out, err = run(capsys, "secret", "wall")
    assert code == 1 and "not built" in err
    assert "sudo" not in out


# ---- doctor WS104


def ws104(home, **cfg):
    full = {"adapter": "ws-secret", **cfg}
    osi = osinfo.OSInfo("linux", "ubuntu 24.04", "x86_64", "systemd", True)
    return doctor._secrets(full, osi, {"WS_HOME": str(home)}, False)


def no_broker(monkeypatch):
    def down(*a, **k):
        raise broker.BrokerUnavailable("no broker answering")

    monkeypatch.setattr(broker, "status", down)


def test_ws104_other_adapters_keep_their_behaviour(home):
    assert ws104(home, adapter="env")[0] == "ok"
    assert ws104(home, adapter="sops-age")[0] == "ok"
    assert ws104(home, adapter="nope")[0] == "fail"
    assert doctor._secrets({}, None, {}, False)[0] == "fail"


def test_ws104_fails_with_no_store_or_no_registry(home, monkeypatch):
    no_broker(monkeypatch)
    status, detail = ws104(home)
    assert status == "fail" and "no registry" in detail and "no store" in detail


def test_ws104_fails_on_loose_permissions(home, monkeypatch):
    no_broker(monkeypatch)
    registry(home, GOOD)
    s = wsecrets.Store(home / "secrets")
    s.write("anthropic", VALUE)
    (s.path / "anthropic").chmod(0o644)
    status, detail = ws104(home)
    assert status == "fail" and "0644" in detail and VALUE not in detail
    (s.path / "anthropic").chmod(0o600)
    s.path.chmod(0o755)
    assert ws104(home)[0] == "fail"


def test_ws104_fails_on_unreadable_registry_or_empty_hosts(home, monkeypatch):
    no_broker(monkeypatch)
    wsecrets.Store(home / "secrets").write("anthropic", VALUE)
    registry(home, "[a\nbroken")
    status, detail = ws104(home)
    assert status == "fail" and "registry unreadable" in detail
    registry(home, "[anthropic]\nhosts = []\n")
    status, detail = ws104(home)
    assert status == "fail" and "anthropic has no hosts" in detail


def test_ws104_warns_on_a_same_user_store(home, monkeypatch):
    no_broker(monkeypatch)
    registry(home, GOOD)
    wsecrets.Store(home / "secrets").write("anthropic", VALUE)
    status, detail = ws104(home)
    assert status == "warn" and "ws secret wall" in detail and "same-user" in detail
    assert VALUE not in detail
    monkeypatch.setattr(broker, "status", lambda sock, **k: {"problems": [], "store_owner_uid": os.geteuid()})
    status, detail = ws104(home)
    assert status == "warn" and "not answering" not in detail


def test_ws104_ok_when_the_store_is_another_users_and_the_broker_answers(home, monkeypatch):
    # the real thing is tests/e2e/test_secret_wall.py; here the permission wall is simulated
    def walled(reg, store):
        raise PermissionError

    monkeypatch.setattr(wsecrets, "health", walled)
    answer = {"problems": [], "store_owner_uid": os.geteuid() + 1, "code_problem": ""}
    monkeypatch.setattr(broker, "status", lambda sock, **k: answer)
    status, detail = ws104(home)
    assert status == "ok" and "broker answering" in detail
    # a walled store is not enough if the broker runs code that another user can edit
    answer = {**answer, "code_problem": "/home/x/ws is owned by x, not root"}
    status, detail = ws104(home)
    assert status == "warn" and "not locked down" in detail and "owned by x" in detail
    answer = {k: v for k, v in answer.items() if k != "code_problem"}
    assert ws104(home)[0] == "warn"
    monkeypatch.setattr(
        broker, "status", lambda sock, **k: {"problems": ["x mode is 0644, not 0600"], "store_owner_uid": 5}
    )
    assert ws104(home)[0] == "fail"
    no_broker(monkeypatch)
    status, detail = ws104(home)
    assert status == "fail" and "not answering" in detail


def test_socket_comes_from_env_then_profile_then_home(home):
    cfg = {"socket": "/somewhere/b.sock"}
    assert wsecrets.socket_path({"WS_HOME": str(home)}, cfg) == Path("/somewhere/b.sock")
    assert wsecrets.socket_path({"WS_HOME": str(home), "WS_BROKER_SOCKET": "/e/b.sock"}, cfg) == Path("/e/b.sock")
    assert wsecrets.socket_path({"WS_HOME": str(home)}, {}) == home / "run" / "broker.sock"
