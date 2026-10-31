"""Model role adapters: anthropic names a model and a key lease, opencode names a provider/model pair. No network."""

from __future__ import annotations

import pytest

from ws.roles import anthropic, opencode


def registry(box, text: str) -> None:
    (box / ".ws" / "secrets.toml").write_text(text, encoding="utf-8")


def test_anthropic_needs_a_model(linux, env):
    assert anthropic.check({}, linux, env, False) == ("fail", "no default model named")
    assert anthropic.check({"default": ""}, linux, env, False)[0] == "fail"


def test_a_subscription_profile_stays_ok_with_no_registry(linux, env):
    """Profiles from before the adapters have only `default`: they were ok and still are."""
    assert anthropic.check({"default": "m-1"}, linux, env, True) == ("ok", "default model m-1 (named; not probed yet)")
    assert anthropic.check({"default": "m-1", "adapter": "anthropic"}, linux, env, True)[0] == "ok"


def test_anthropic_with_a_key_warns_until_the_registry_can_lease_it(linux, env):
    status, detail = anthropic.check({"default": "m-1", "key": "anthropic"}, linux, env, True)
    assert status == "warn" and "m-1" in detail and "not probed" in detail  # no registry yet


def test_anthropic_looks_at_the_registry_never_the_store(box, linux, env, monkeypatch):
    registry(box, '[anthropic]\nhosts = ["api.anthropic.com"]\n')

    def boom(*a, **k):
        raise AssertionError("the store was read")

    monkeypatch.setattr("ws.secrets.Store.read", boom)
    status, detail = anthropic.check({"default": "m-1", "key": "anthropic"}, linux, env, True)
    assert status == "ok" and "leased through the broker" in detail


@pytest.mark.parametrize(
    ("text", "key", "word"),
    [
        ('[other]\nhosts = ["api.anthropic.com"]\n', "anthropic", "no [anthropic]"),
        ('[anthropic]\nhosts = ["example.org"]\n', "anthropic", "not allowed at api.anthropic.com"),
        ("[anthropic\n", "anthropic", "does not read"),
    ],
)
def test_anthropic_warns_on_a_registry_that_cannot_lease(box, linux, env, text, key, word):
    registry(box, text)
    status, detail = anthropic.check({"default": "m", "key": key}, linux, env, False)
    assert status == "warn" and word in detail


def test_anthropic_key_must_be_a_secret_name(linux, env):
    assert anthropic.check({"default": "m", "key": "../x"}, linux, env, False)[0] == "fail"


@pytest.mark.parametrize(
    ("cfg", "want"),
    [
        ({"default": "anthropic/m1"}, ("anthropic", "m1")),
        ({"default": "m1", "provider": "local"}, ("local", "m1")),
        ({"default": "local/m1", "provider": "local"}, ("local", "m1")),
        ({"default": "m1"}, None),
        ({"default": "/m1"}, None),
        ({"default": 3}, None),
    ],
)
def test_opencode_pair(cfg, want):
    assert opencode.pair(cfg) == want


def test_opencode_names_the_provider(linux, env):
    assert opencode.check({}, linux, env, False) == ("fail", "no default model named")
    status, detail = opencode.check({"default": "m1"}, linux, env, False)
    assert status == "fail" and "provider" in detail


def test_opencode_skips_the_list_offline_or_by_choice(linux, env, monkeypatch):
    monkeypatch.setattr(opencode, "list_models", lambda *a: pytest.fail("ran opencode"))
    cfg = {"default": "p/m"}
    assert "--offline" in opencode.check(cfg, linux, env, False)[1]
    assert opencode.check({**cfg, "check_models": False}, linux, env, True)[0] == "ok"


def test_opencode_not_on_path_is_ok_but_says_so(linux, env, tmp_path):
    status, detail = opencode.check({"default": "p/m"}, linux, {**env, "PATH": str(tmp_path)}, True)
    assert status == "ok" and "not on PATH" in detail


def fake_opencode(tmp_path):
    exe = tmp_path / "opencode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return {"PATH": str(tmp_path)}


def test_opencode_must_list_the_model(linux, env, tmp_path, monkeypatch):
    on_path = {**env, **fake_opencode(tmp_path)}
    monkeypatch.setattr(opencode, "list_models", lambda exe, e: ["p/m", "q/n"])
    assert opencode.check({"default": "p/m"}, linux, on_path, True) == ("ok", "default model p/m via OpenCode (listed)")
    status, detail = opencode.check({"default": "p/zzz"}, linux, on_path, True)
    assert status == "fail" and "does not list p/zzz" in detail
    monkeypatch.setattr(opencode, "list_models", lambda exe, e: None)
    assert opencode.check({"default": "p/m"}, linux, on_path, True)[0] == "warn"


def test_opencode_list_models_reads_stdout(tmp_path):
    exe = tmp_path / "opencode"
    exe.write_text("#!/bin/sh\nprintf 'p/m\\n\\nq/n\\n'\n")
    exe.chmod(0o755)
    assert opencode.list_models(str(exe), {}) == ["p/m", "q/n"]
    exe.write_text("#!/bin/sh\nexit 3\n")
    assert opencode.list_models(str(exe), {}) is None
    assert opencode.list_models(str(tmp_path / "missing"), {}) is None


def test_model_steps_write_nothing(box, env):
    from ws import install

    answers = install.Answers()
    assert anthropic.steps({}, answers, env) == [] and opencode.steps({}, answers, env) == []
