"""The role registry: adapters are found by scanning ws/roles, every one has the same shape, and doctor holds no list
of its own."""

from __future__ import annotations

import inspect
import tomllib

import pytest

from ws import doctor, install, roles
from ws.profile import profile_path


def test_every_adapter_has_the_one_shape():
    found = roles.all_roles()
    assert {"model", "secrets", "memory", "scheduler", "notify", "backup"} <= set(found)
    for role, names in found.items():
        for name, mod in names.items():
            assert (mod.ROLE, mod.NAME) == (role, name)
            assert list(inspect.signature(mod.check).parameters) == ["cfg", "osi", "env", "network"]
            assert list(inspect.signature(mod.steps).parameters) == ["cfg", "answers", "env"]


def test_the_named_adapters_are_all_there():
    got = {r: set(a) for r, a in roles.all_roles().items()}
    assert got["model"] == {"anthropic", "opencode"}
    assert got["memory"] == {"markdown-git"}
    assert got["scheduler"] == {"systemd", "launchd"}
    assert got["notify"] == {"ntfy", "none"}
    assert got["backup"] == {"restic"}
    assert got["secrets"] == {"ws-secret", "sops-age", "env"}


def test_get_names_what_is_known():
    assert roles.get("backup", "restic").NAME == "restic"
    with pytest.raises(KeyError, match="known: restic"):
        roles.get("backup", "tarsnap")


def test_check_picks_the_adapter_and_names_the_known_ones(linux):
    assert roles.check("notify", {"adapter": "none"}, linux, {}, False) == ("ok", "off (no pushes)")
    status, detail = roles.check("notify", {"adapter": "pigeon"}, linux, {}, False)
    assert status == "fail" and "known: none, ntfy" in detail
    assert roles.check("notify", {}, linux, {}, False) == ("fail", "no adapter named")
    assert roles.check("nothing", {"adapter": "x"}, linux, {}, False)[0] == "fail"


def test_doctor_asks_the_registry_not_a_list(linux, monkeypatch):
    seen = set()
    monkeypatch.setattr(roles, "adapters", lambda role: seen.add(role) or {})
    for r in doctor.ROLES:
        if r.name in ("secrets", "memory", "scheduler", "notify", "backup"):
            status, detail = r.check({"adapter": "x"}, linux, {}, False)
            assert status == "fail" and "unknown adapter 'x'" in detail
    assert seen == {"secrets", "memory", "scheduler", "notify", "backup"}


def test_a_model_table_from_before_the_adapters_still_passes(linux, env):
    model = next(r for r in doctor.ROLES if r.name == "model")
    status, detail = model.check({"adapter": "subscription", "default": "m-1"}, linux, env, False)
    assert status == "ok" and "m-1" in detail and "no check of its own" in detail
    assert model.check({"adapter": "subscription"}, linux, env, False)[0] == "fail"  # still needs a model


def test_role_ids_do_not_change():
    assert [(r.id, r.name) for r in doctor.ROLES] == [
        ("WS101", "runner"),
        ("WS102", "model"),
        ("WS103", "budget"),
        ("WS104", "secrets"),
        ("WS105", "memory"),
        ("WS106", "notes"),
        ("WS107", "scheduler"),
        ("WS108", "notify"),
        ("WS109", "backup"),
        ("WS110", "usage"),
    ]


def test_doctor_end_to_end_uses_the_adapters(box, linux, write_profile):
    write_profile(
        {
            "roles": {
                "model": {"adapter": "opencode", "default": "local/qwen", "check_models": False},
                "notify": {"adapter": "ntfy", "url": "http://ntfy.example.org/t"},
                "backup": {"adapter": "restic"},
            }
        }
    )
    checks = {c.id: c for c in doctor.run_checks(linux, profile_path({"WS_HOME": str(box / ".ws")}), network=False)}
    assert checks["WS102"].status == "ok" and "local/qwen" in checks["WS102"].detail
    assert checks["WS108"].status == "fail" and "https" in checks["WS108"].detail
    assert checks["WS109"].status == "fail" and "repo" in checks["WS109"].detail


def test_doctor_fix_examples_for_adapter_roles_pass_their_own_check(box, linux, write_profile, tmp_path):
    """The table doctor tells you to add must not itself fail."""
    exe = tmp_path / "bin" / "restic"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    store = box / ".ws" / "secrets"
    store.mkdir(mode=0o700)
    (store / "restic-password").write_text("pw")
    (store / "restic-password").chmod(0o600)
    write_profile({"roles": {"memory": {"path": str(box)}}})
    env = {"HOME": str(box), "WS_HOME": str(box / ".ws"), "PATH": str(exe.parent)}
    for r in doctor.ROLES:
        if r.name in ("model", "scheduler", "notify", "backup"):
            cfg = tomllib.loads(r.example.replace(" and ", "\n").replace(", ", "\n"))
            status, detail = r.check(cfg, linux, env, False)
            assert status in ("ok", "warn"), (r.name, status, detail)


def test_adapter_steps_are_install_steps(box, env):
    answers = install.Answers(memory=str(box / "mem"))
    for names in roles.all_roles().values():
        for mod in names.values():
            for step in mod.steps({}, answers, env):
                assert isinstance(step, install.Step)
