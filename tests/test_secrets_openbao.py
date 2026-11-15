"""The optional OpenBao role, against a fake KV v2 server on loopback. Nothing real is contacted or installed."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ws import secrets as wsecrets
from ws.roles import FAIL, OK, secrets_openbao

VALUE = "planted-" + os.urandom(12).hex()
TOKEN = "tok-" + os.urandom(8).hex()


class _Fake(BaseHTTPRequestHandler):
    data = {"demo": VALUE, "bad": 5}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Fake.seen.append((self.path, self.headers.get("X-Vault-Token")))
        if self.headers.get("X-Vault-Token") != TOKEN:
            return self._send(403, {})
        if "/metadata/" in self.path:
            return self._send(200, {"data": {"keys": sorted([*_Fake.data, "../x"])}})
        name = self.path.split("/data/ws/")[-1].split("?")[0]
        if name == "redir":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/")
            self.end_headers()
            return None
        if name not in _Fake.data:
            return self._send(404, {})
        return self._send(200, {"data": {"data": {"value": _Fake.data[name]}}})

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def server():
    _Fake.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path, server):
    tf = tmp_path / "bao-token"
    tf.write_text(TOKEN + "\n")
    tf.chmod(0o600)
    return {"adapter": "openbao", "addr": server, "token_file": str(tf)}


def test_a_value_is_read_with_the_token_and_an_unset_name_is_not_found(cfg):
    s = secrets_openbao.store_from_cfg(cfg)
    assert s.read("demo") == VALUE
    assert _Fake.seen[-1][1] == TOKEN
    with pytest.raises(FileNotFoundError):
        s.read("missing")
    assert s.names() == ["bad", "demo"]  # a name the registry could not hold is dropped


def test_a_bad_entry_or_a_wrong_token_never_shows_a_value(cfg, tmp_path):
    s = secrets_openbao.store_from_cfg(cfg)
    with pytest.raises(wsecrets.SecretsError) as e:
        s.read("bad")
    assert VALUE not in str(e.value)
    s.token_file.write_text("wrong\n")
    s.token_file.chmod(0o600)
    with pytest.raises(wsecrets.SecretsError) as e:
        s.read("demo")
    assert "403" in str(e.value) and VALUE not in str(e.value) and "wrong" not in str(e.value)


def test_a_loose_token_file_is_refused_and_reported(cfg):
    s = secrets_openbao.store_from_cfg(cfg)
    s.token_file.chmod(0o644)
    with pytest.raises(wsecrets.SecretsError, match="open to other users"):
        s.read("demo")
    assert s.loose() and s.problems() == s.loose()


def test_a_redirect_is_not_followed_so_the_token_stays_put(cfg):
    s = secrets_openbao.store_from_cfg(cfg)
    with pytest.raises(wsecrets.SecretsError):
        s.read("redir")


def test_addr_must_be_https_or_loopback_and_every_key_is_needed(cfg, tmp_path):
    for bad in ("http://bao.example.org:8200", "ftp://x", "https://u:p@bao.example.org", "https://"):
        with pytest.raises(wsecrets.SecretsError):
            secrets_openbao.store_from_cfg({**cfg, "addr": bad})
    with pytest.raises(wsecrets.SecretsError):
        secrets_openbao.store_from_cfg({"adapter": "openbao", "addr": "https://bao.example.org"})
    assert secrets_openbao.store_from_cfg({**cfg, "addr": "https://bao.example.org:8200"})


def test_doctor_check_and_the_roles_scan(cfg):
    from ws import roles

    assert "openbao" in roles.adapters("secrets")
    assert secrets_openbao.check(cfg, None, {}, True)[0] == OK
    assert secrets_openbao.check({"adapter": "openbao"}, None, {}, False)[0] == FAIL
    assert secrets_openbao.check({**cfg, "token_file": "/nonexistent/t"}, None, {}, False)[0] == FAIL


def test_health_of_a_remote_store_and_set_is_refused(cfg, tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    h.mkdir()
    monkeypatch.setenv("WS_HOME", str(h))
    reg = wsecrets.registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text('[demo]\nhosts = ["api.example.com"]\n')
    problems, owner = wsecrets.health(reg, secrets_openbao.store_from_cfg(cfg))
    assert problems == [f"{n} is in the store but not in the registry" for n in ["bad"]]
    assert owner == os.geteuid()
    monkeypatch.setattr(wsecrets, "_profile_secrets", lambda env: cfg)
    with pytest.raises(wsecrets.SecretsError, match="OpenBao"):
        wsecrets.local_store()
    assert isinstance(wsecrets.open_store(), secrets_openbao.OpenBaoStore)


def test_the_default_needs_no_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    assert isinstance(wsecrets.open_store(), wsecrets.Store)
